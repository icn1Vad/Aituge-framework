import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from llama_index.core.tools.function_tool import FunctionTool

from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.agent_registry.defaults import (
    LEGACY_MEDIA_WRITER_AGENT_PROMPT,
    build_default_agent_profiles,
)
from scheduling.agent_registry.models import AgentProfileEntity
from scheduling.api import create_scheduling_router
from scheduling.main_agent.service import (
    MainAgentService,
    _ScopedSingleAgentService,
    _delegation_mode_config,
    _main_runtime_profile,
    _require_successful_managed_tool,
    _select_workspace_tools,
)
from scheduling.main_agent.schemas import MainAgentChatRequest
from scheduling.main_agent.store import (
    MainAgentSessionStore,
    ManagedSingleAgentStore,
    ScriptWorkspaceStore,
)
from scheduling.scheduler import (
    RuntimeContextBlock,
    SchedulingRuntimeContext,
    SchedulingRuntimeOptions,
    SchedulingService,
)
from service.agent import SingleAgentStreamEvent
from task_manager.schemas import TaskCreateRequest
from task_manager.models import TaskMemoryEntity
from task_manager.service import TaskManagerService
from db.db_context import create_db_session, init_db, reset_engine_for_test


def _options(tmp_path):
    return SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")


def test_registry_mode_config_selects_workspace_tools_and_required_success():
    async def placeholder(value: str = "") -> str:
        return value

    tools = [
        FunctionTool.from_defaults(async_fn=placeholder, name=name, description="test")
        for name in [
            "read_script_workspace",
            "write_script_workspace",
            "write_storyboard_workspace",
        ]
    ]

    profiles = {profile.agent_id: profile for profile in build_default_agent_profiles()}
    writer = profiles["media-writer-agent"]
    storyboard = profiles["media-storyboard-agent"]
    writer_consult = _delegation_mode_config(writer, "consult")
    writer_delegate = _delegation_mode_config(writer, "delegate")
    storyboard_delegate = _delegation_mode_config(storyboard, "delegate")

    assert writer_consult == {
        "skill_package": "media-writer-consult-package",
        "extra_tools": [],
        "workspace_tools": ["read_script_workspace"],
        "required_success_tool": "",
    }
    assert writer.default_tools == []
    assert writer_delegate["extra_tools"] == ["media_master_library"]
    assert storyboard_delegate["extra_tools"] == []
    assert [
        tool.metadata.name
        for tool in _select_workspace_tools(
            tools,
            writer_delegate["workspace_tools"],
            agent_id=writer.agent_id,
            mode="delegate",
        )
    ] == ["read_script_workspace", "write_script_workspace"]
    assert [
        tool.metadata.name
        for tool in _select_workspace_tools(
            tools,
            storyboard_delegate["workspace_tools"],
            agent_id=storyboard.agent_id,
            mode="delegate",
        )
    ] == ["read_script_workspace", "write_storyboard_workspace"]

    with pytest.raises(ValueError, match="completed without a successful"):
        _require_successful_managed_tool(
            "media-writer-agent",
            "delegate",
            "write_script_workspace",
            set(),
        )

    with pytest.raises(ValueError, match="requires unavailable Workspace tools"):
        _select_workspace_tools(
            tools[:1],
            writer_delegate["workspace_tools"],
            agent_id=writer.agent_id,
            mode="delegate",
        )

    broken = AgentProfileEntity(
        agent_id="broken-agent",
        name="Broken Agent",
        runtime_config_json=json.dumps(
            {
                "delegation": {
                    "enabled": True,
                    "modes": {
                        "consult": {
                            "skill_package": "",
                            "workspace_tools": [],
                        }
                    },
                }
            }
        ),
    )
    with pytest.raises(ValueError, match="no Skill Package configured"):
        _delegation_mode_config(broken, "consult")


def test_default_registry_backfills_modes_without_overwriting_private_config(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            writer = await get_agent_profile(session, "media-writer-agent")
            assert writer is not None
            writer.runtime_config_json = json.dumps(
                {
                    "delegation": {
                        "enabled": True,
                        "use_when": "private writer rule",
                        "modes": {
                            "consult": {
                                "skill_package": "private-consult-package",
                                "workspace_tools": [],
                            }
                        },
                    }
                }
            )
            writer.system_prompt = LEGACY_MEDIA_WRITER_AGENT_PROMPT
            writer.default_tools_json = json.dumps(["media_master_library"])
            session.add(writer)
            await session.commit()

            await ensure_default_agent_profiles(session)
            await session.refresh(writer)

        delegation = writer.runtime_config["delegation"]
        assert writer.system_prompt == "You are Media Writer Agent."
        assert writer.default_tools == []
        assert delegation["use_when"] == "private writer rule"
        assert delegation["modes"]["consult"] == {
            "skill_package": "private-consult-package",
            "workspace_tools": [],
        }
        assert delegation["modes"]["delegate"]["skill_package"] == (
            "media-writer-delegate-package"
        )
        assert delegation["modes"]["delegate"]["extra_tools"] == [
            "media_master_library"
        ]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_workspace_write_tools_are_terminal_and_return_small_receipts(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'workspace-tools.db'}")
        reset_engine_for_test()
        await init_db()
        workspace = await ScriptWorkspaceStore().create(script_text="v1", user_id="main-user")
        task_service = TaskManagerService(_options(tmp_path))
        task = await task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.generate",
                task_key="media_script",
                input_payload={
                    "workspace_id": workspace.id,
                    "instruction": "write",
                    "operation": "interact",
                },
                user_id="main-user",
                stream=False,
                metadata={"existing_marker": "keep"},
            )
        )
        task = await task_service.begin_external_task(task.id, user_id="main-user")
        tools = MainAgentService(_options(tmp_path))._workspace_tools(workspace.id, task)
        by_name = {tool.metadata.name: tool for tool in tools}

        assert by_name["read_script_workspace"].metadata.return_direct is False
        assert by_name["write_script_workspace"].metadata.return_direct is True
        assert by_name["write_script_and_storyboard_workspace"].metadata.return_direct is True
        assert by_name["write_storyboard_workspace"].metadata.return_direct is True
        storyboard_schema = by_name["write_storyboard_workspace"].metadata.get_parameters_dict()
        assert storyboard_schema["required"] == [
            "storyboard",
            "storyboard_plan",
            "visual_direction",
            "warnings",
        ]
        assert storyboard_schema["properties"]["storyboard"]["type"] == "array"

        output = await by_name["write_script_workspace"].acall(
            script_text="final script",
            role_id="role_yanjie",
            strategy_id="strategy_path",
            template_id="template_three_step",
            script_type_id="script_type_advice",
            script_example_ids=["example_1"],
            risk_rule_ids=["risk_policy"],
            replace_reason="Initial selection for this topic.",
        )
        receipt = json.loads(output.content)
        assert receipt == {
            "status": "saved",
            "workspace_id": workspace.id,
            "field": "script_text",
            "character_count": len("final script"),
        }
        assert "final script" not in output.content
        updated_task = await task_service.get_task(task.id)
        assert updated_task is not None
        assert updated_task.metadata_json["existing_marker"] == "keep"
        assert updated_task.metadata_json["master_library_usage"] == {
            "role_id": "role_yanjie",
            "strategy_id": "strategy_path",
            "template_id": "template_three_step",
            "script_type_id": "script_type_advice",
            "script_example_ids": ["example_1"],
            "risk_rule_ids": ["risk_policy"],
            "replace_reason": "Initial selection for this topic.",
        }

        storyboard_output = await by_name["write_storyboard_workspace"].acall(
            storyboard=[
                {
                    "time": "0-3s",
                    "scene": "Indoor close-up",
                    "shot": "Static close-up",
                    "action": "Look into the camera",
                    "voiceover": 'Your rights finally have a "backstop".',
                    "subtitle_focus": "Know your rights",
                    "visual_prompt": "A veteran speaking directly to camera",
                }
            ],
            storyboard_plan={"total_shots": 1},
            visual_direction=["Keep the framing stable."],
            warnings=[],
        )
        storyboard_receipt = json.loads(storyboard_output.content)
        saved_workspace = await ScriptWorkspaceStore().get(
            workspace.id,
            user_id="main-user",
        )
        assert saved_workspace is not None
        assert saved_workspace.script_text == "final script"
        assert storyboard_receipt == {
            "status": "saved",
            "workspace_id": workspace.id,
            "field": "storyboard_text",
            "character_count": len(saved_workspace.storyboard_text),
        }
        saved_storyboard = json.loads(saved_workspace.storyboard_text)
        assert saved_storyboard["storyboard"][0]["voiceover"] == (
            'Your rights finally have a "backstop".'
        )
        assert saved_storyboard["storyboard_plan"] == {"total_shots": 1}

        atomic_output = await by_name["write_script_and_storyboard_workspace"].acall(
            script_text="atomically revised script",
            storyboard_updates=[
                {
                    "shot_index": 0,
                    "replacement": {
                        "time": "0-3s",
                        "scene": "Indoor close-up",
                        "shot": "Slow push-in",
                        "action": "Point to the updated subtitle",
                        "voiceover": "The revised opening is now synchronized.",
                        "subtitle_focus": "Revised opening",
                        "visual_prompt": "A veteran presenting the revised opening",
                    },
                }
            ],
            replace_reason="Update the opening sentence and its matching shot.",
        )
        atomic_receipt = json.loads(atomic_output.content)
        atomically_saved_workspace = await ScriptWorkspaceStore().get(
            workspace.id,
            user_id="main-user",
        )
        assert atomically_saved_workspace is not None
        assert atomically_saved_workspace.script_text == "atomically revised script"
        atomic_storyboard = json.loads(atomically_saved_workspace.storyboard_text)
        assert atomic_storyboard["storyboard"][0]["shot"] == "Slow push-in"
        assert atomic_storyboard["storyboard"][0]["voiceover"] == (
            "The revised opening is now synchronized."
        )
        assert atomic_storyboard["storyboard_plan"] == {"total_shots": 1}
        assert atomic_receipt == {
            "status": "saved",
            "workspace_id": workspace.id,
            "fields": ["script_text", "storyboard_text"],
            "shot_indexes": [0],
            "script_character_count": len("atomically revised script"),
            "storyboard_character_count": len(atomically_saved_workspace.storyboard_text),
        }

        await by_name["write_script_workspace"].acall(script_text="revised script")
        revised_workspace = await ScriptWorkspaceStore().get(
            workspace.id,
            user_id="main-user",
        )
        assert revised_workspace is not None
        assert revised_workspace.script_text == "revised script"
        assert (
            revised_workspace.storyboard_text
            == atomically_saved_workspace.storyboard_text
        )
        preserved_task = await task_service.get_task(task.id)
        assert preserved_task is not None
        assert preserved_task.metadata_json["existing_marker"] == "keep"
        assert preserved_task.metadata_json["master_library_usage"] == {
            **updated_task.metadata_json["master_library_usage"],
            "replace_reason": "Update the opening sentence and its matching shot.",
        }

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_atomic_workspace_write_rejects_invalid_shot_indexes(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'workspace-index-validation.db'}",
        )
        reset_engine_for_test()
        await init_db()
        storyboard_text = json.dumps(
            {
                "storyboard": [
                    {
                        "time": "0-3s",
                        "scene": "Opening",
                        "shot": "Close-up",
                        "action": "Look into camera",
                        "voiceover": "Original opening.",
                        "subtitle_focus": "Opening",
                        "visual_prompt": "Direct-to-camera opening",
                    },
                    {
                        "time": "3-8s",
                        "scene": "Explanation",
                        "shot": "Medium shot",
                        "action": "Point to the document",
                        "voiceover": "Original explanation.",
                        "subtitle_focus": "Explanation",
                        "visual_prompt": "Presenter explains a document",
                    },
                ],
                "storyboard_plan": {"total_shots": 2},
                "visual_direction": ["Keep the framing stable."],
                "warnings": [],
            },
            ensure_ascii=False,
        )
        workspace = await ScriptWorkspaceStore().create(
            script_text="original script",
            storyboard_text=storyboard_text,
            user_id="main-user",
        )
        task_service = TaskManagerService(_options(tmp_path))
        task = await task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.generate",
                task_key="media_script",
                input_payload={
                    "workspace_id": workspace.id,
                    "instruction": "revise",
                    "operation": "interact",
                },
                user_id="main-user",
            )
        )
        task = await task_service.begin_external_task(task.id, user_id="main-user")
        tools = MainAgentService(_options(tmp_path))._workspace_tools(workspace.id, task)
        atomic_tool = {
            tool.metadata.name: tool for tool in tools
        }["write_script_and_storyboard_workspace"]
        replacement = {
            "time": "0-3s",
            "scene": "Opening",
            "shot": "Close-up",
            "action": "Look into camera",
            "voiceover": "Revised opening.",
            "subtitle_focus": "Revised opening",
            "visual_prompt": "Direct-to-camera revised opening",
        }

        with pytest.raises(ValueError, match="unique shot_index"):
            await atomic_tool.acall(
                script_text="duplicate index script",
                storyboard_updates=[
                    {"shot_index": 0, "replacement": replacement},
                    {"shot_index": 0, "replacement": replacement},
                ],
            )
        with pytest.raises(ValueError, match="out-of-range"):
            await atomic_tool.acall(
                script_text="out of range script",
                storyboard_updates=[
                    {"shot_index": 2, "replacement": replacement},
                ],
            )
        with pytest.raises(ValueError):
            await atomic_tool.acall(
                script_text="too many updates script",
                storyboard_updates=[
                    {"shot_index": 0, "replacement": replacement},
                    {"shot_index": 1, "replacement": replacement},
                    {"shot_index": 0, "replacement": replacement},
                    {"shot_index": 1, "replacement": replacement},
                ],
            )

        unchanged = await ScriptWorkspaceStore().get(
            workspace.id,
            user_id="main-user",
        )
        assert unchanged is not None
        assert unchanged.script_text == "original script"
        assert unchanged.storyboard_text == storyboard_text

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_main_agent_tools_do_not_depend_on_legacy_session_phase(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'main-agent.db'}")
        reset_engine_for_test()
        await init_db()

        service = MainAgentService(_options(tmp_path))
        assert isinstance(service, SchedulingService)

        main_session = await MainAgentSessionStore().get_or_create(
            "main-session",
            user_id="main-user",
        )
        new_tools = service._delegation_tools(
            primary_profile=_main_runtime_profile(),
            primary_session_id="main-session",
            user_id="main-user",
            model=None,
            task=None,
            workspace_tools=[],
        )
        assert [tool.metadata.name for tool in new_tools] == [
            "consult_agent",
            "delegate_agent",
            "list_delegatable_agents",
            "list_active_agents",
        ]

        main_session = await MainAgentSessionStore().update(
            "main-session",
            user_id="main-user",
            thread_id="main-thread",
            phase="active",
        )
        active_tools = service._delegation_tools(
            primary_profile=_main_runtime_profile(),
            primary_session_id="main-session",
            user_id="main-user",
            model=None,
            task=None,
            workspace_tools=[],
        )
        assert [tool.metadata.name for tool in active_tools] == [
            tool.metadata.name for tool in new_tools
        ]

        workspace = await ScriptWorkspaceStore().create(
            script_text="初始脚本",
            user_id="main-user",
        )
        managed = await ManagedSingleAgentStore().create(
            primary_session_id="main-session",
            primary_agent_id="default-single-agent",
            agent_id="media-writer-agent",
            user_id="main-user",
        )
        rows = await ManagedSingleAgentStore().list(
            primary_session_id="main-session",
            user_id="main-user",
        )

        assert rows[0].instance_id == managed.instance_id
        assert main_session.thread_id == "main-thread"
        assert main_session.phase == "active"
        assert workspace.to_read_model()["script_text"] == "初始脚本"
        assert "agent_id" not in workspace.to_read_model()

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_main_agent_prepare_uses_task_package_and_direct_workspace_tools(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'main-prepare.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            session.add(
                TaskMemoryEntity(
                    user_id="main-user",
                    task_key="media_script",
                    content="Open with the conclusion and keep the tone conversational.",
                )
            )
            await session.commit()
        workspace = await ScriptWorkspaceStore().create(
            script_text="初始脚本",
            user_id="main-user",
        )
        service = MainAgentService(_options(tmp_path))
        instruction = "做一个简单修改"
        task = await service.task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.generate",
                task_key="media_script",
                input_payload={
                    "workspace_id": workspace.id,
                    "instruction": instruction,
                    "operation": "interact",
                },
                user_id="main-user",
                stream=False,
            )
        )
        prepared = await service._prepare_turn(
            MainAgentChatRequest(
                message=instruction,
                workspace_id=workspace.id,
                task_id=task.id,
                user_id="main-user",
                stream=False,
            ),
            stream=False,
        )

        assert prepared["scoped_request"].skill_package == "media-script-main-agent-package"
        assert prepared["task"].id == task.id
        assert prepared["task"].status == "running"
        prepared_prompt = prepared["runtime_context"].render_prompt()
        assert "Stable task key: media_script" in prepared_prompt
        assert "Open with the conclusion" in prepared_prompt
        assert "Open with the conclusion" in service.runtime_context.render_prompt()
        assert [tool.metadata.name for tool in service.runtime_tools] == [
            "consult_agent",
            "delegate_agent",
            "list_delegatable_agents",
            "list_active_agents",
            "read_script_workspace",
            "write_script_and_storyboard_workspace",
        ]
        await service._fail_prepared_task(prepared, RuntimeError("test cleanup"))

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_main_agent_workspace_requires_matching_formal_task(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'main-contract.db'}")
        reset_engine_for_test()
        await init_db()
        workspace = await ScriptWorkspaceStore().create(
            script_text="existing script",
            user_id="main-user",
        )
        service = MainAgentService(_options(tmp_path))

        with pytest.raises(ValueError, match="task_id is required"):
            await service._prepare_turn(
                MainAgentChatRequest(
                    message="edit",
                    workspace_id=workspace.id,
                    user_id="main-user",
                ),
                stream=False,
            )

        with pytest.raises(ValueError, match="requires task_key 'media_script'"):
            await service.task_service.create_task(
                TaskCreateRequest(
                    task_type="media.script.generate",
                    task_key="wrong-key",
                    input_payload={
                        "workspace_id": workspace.id,
                        "instruction": "edit",
                        "operation": "interact",
                    },
                    user_id="main-user",
                )
            )

        task = await service.task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.generate",
                task_key="media_script",
                input_payload={
                    "workspace_id": workspace.id,
                    "instruction": "edit",
                    "operation": "interact",
                },
                user_id="main-user",
            )
        )
        with pytest.raises(ValueError, match="instruction does not match"):
            await service._prepare_turn(
                MainAgentChatRequest(
                    message="different instruction",
                    workspace_id=workspace.id,
                    task_id=task.id,
                    user_id="main-user",
                ),
                stream=False,
            )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_generate_task_requires_script_and_storyboard_workspace_outputs(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'main-generate.db'}")
        reset_engine_for_test()
        await init_db()
        workspace = await ScriptWorkspaceStore().create(script_text="", user_id="main-user")
        service = MainAgentService(_options(tmp_path))
        instruction = "Generate a complete script and storyboard."
        task = await service.task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.generate",
                task_key="media_script",
                input_payload={
                    "workspace_id": workspace.id,
                    "instruction": instruction,
                    "operation": "generate",
                },
                user_id="main-user",
            )
        )
        prepared = await service._prepare_turn(
            MainAgentChatRequest(
                message=instruction,
                workspace_id=workspace.id,
                task_id=task.id,
                user_id="main-user",
            ),
            stream=False,
        )
        assert [tool.metadata.name for tool in service.runtime_tools] == [
            "consult_agent",
            "delegate_agent",
            "list_delegatable_agents",
            "list_active_agents",
            "read_script_workspace",
        ]

        with pytest.raises(ValueError, match="delegate both Writer and Storyboard"):
            await service._finalize_turn(
                prepared,
                thread_id="main-thread",
                session_id="main-session",
                response_content="done",
            )
        service.subagent_outputs = [
            {"mode": "delegate", "agent_id": "media-writer-agent"},
            {"mode": "delegate", "agent_id": "media-storyboard-agent"},
        ]
        with pytest.raises(ValueError, match="save both script_text and storyboard_text"):
            await service._finalize_turn(
                prepared,
                thread_id="main-thread",
                session_id="main-session",
                response_content="done",
            )
        await service._fail_prepared_task(prepared, RuntimeError("missing outputs"))
        failed = await service.task_service.get_task(task.id)
        assert failed is not None
        assert failed.status == "failed"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_main_agent_routes_expose_catalog_and_workspace(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
        reset_engine_for_test()
        await init_db()

        async def fake_main_stream(self, request):
            yield {
                "event": "metadata",
                "data": {
                    "event": "metadata",
                    "thread_id": "main-thread",
                    "session_id": request.session_id or "main-session",
                },
            }
            yield {
                "event": "final",
                "data": {
                    "event": "final",
                    "thread_id": "main-thread",
                    "session_id": request.session_id or "main-session",
                    "data": {"content": "done"},
                },
            }

        monkeypatch.setattr(MainAgentService, "stream_chat", fake_main_stream)
        app = FastAPI()
        app.include_router(create_scheduling_router(_options(tmp_path)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            catalog = await client.get("/scheduling/agents/delegatable")
            assert catalog.status_code == 200
            writer = next(
                row
                for row in catalog.json()["agents"]
                if row["agent_id"] == "media-writer-agent"
            )
            assert writer["modes"]["consult"] == {
                "skill_package": "media-writer-consult-package",
                "workspace_tools": ["read_script_workspace"],
            }
            assert writer["modes"]["delegate"] == {
                "skill_package": "media-writer-delegate-package",
                "workspace_tools": [
                    "read_script_workspace",
                    "write_script_workspace",
                ],
            }
            assert not any(
                row["agent_id"] == "media-main-agent"
                for row in catalog.json()["agents"]
            )
            wrong_scheduler = await client.post(
                "/scheduling/agents/media-main-agent/chat",
                json={"message": "wrong scheduler", "user_id": "main-user", "stream": False},
            )
            assert wrong_scheduler.status_code == 404
            legacy_main_route = await client.post(
                "/scheduling/main-agents/default-single-agent/chat",
                json={"message": "legacy route", "user_id": "main-user", "stream": False},
            )
            assert legacy_main_route.status_code == 404
            managed = await client.get(
                "/scheduling/main/managed-agents",
                params={"primary_session_id": "main-session", "user_id": "main-user"},
            )
            assert managed.status_code == 200
            assert managed.json() == {"managed_agents": []}
            created = await client.post(
                "/scheduling/script-workspaces",
                json={"script_text": "共享脚本", "user_id": "main-user"},
            )
            assert created.status_code == 200
            workspace_id = created.json()["workspace"]["id"]
            loaded = await client.get(
                f"/scheduling/script-workspaces/{workspace_id}",
                params={"user_id": "main-user"},
            )
            assert loaded.json()["workspace"]["script_text"] == "共享脚本"

            streamed = await client.post(
                "/scheduling/main/chat",
                json={"message": "stream", "user_id": "main-user", "stream": True},
            )
            assert streamed.status_code == 200
            assert "event: metadata" in streamed.text
            assert "event: final" in streamed.text
            assert '"content": "done"' in streamed.text

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_same_managed_single_agent_can_consult_then_delegate(tmp_path, monkeypatch):
    calls = []

    async def fake_child_chat(self, profile, request):
        calls.append(
            {
                "thread_id": request.thread_id,
                "session_id": request.session_id,
                "tools": [tool.metadata.name for tool in self.runtime_tools],
                "extra_tools": request.extra_tools,
                "skill_package": request.skill_package,
                "message": request.message,
                "runtime_prompt": self.runtime_context.render_prompt(),
            }
        )
        return {
            "thread_id": request.thread_id or "child-thread-1",
            "session_id": request.session_id,
            "response": {
                "choices": [{"message": {"content": f"reply-{len(calls)}"}}],
                "steps": [
                    {
                        "tool": {"function": {"name": tool.metadata.name}},
                        "result": "saved",
                        "error": None,
                    }
                    for tool in self.runtime_tools
                    if tool.metadata.name.startswith("write_")
                ],
            },
        }

    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'managed.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(_ScopedSingleAgentService, "chat", fake_child_chat)
        await init_db()
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            primary = await get_agent_profile(session, "default-single-agent")
        assert primary is not None

        service = MainAgentService(_options(tmp_path))

        async def read_demo() -> str:
            return "workspace"

        async def write_demo(script_text: str) -> str:
            return script_text

        read_tool = FunctionTool.from_defaults(
            async_fn=read_demo,
            name="read_script_workspace",
            description="test tool",
        )
        write_tool = FunctionTool.from_defaults(
            async_fn=write_demo,
            name="write_script_workspace",
            description="test tool",
        )
        workspace_tools = [read_tool, write_tool]
        runtime_context = SchedulingRuntimeContext(
            blocks=(
                RuntimeContextBlock(
                    kind="task_memory",
                    content="# Task Memory\nPrefer a direct opening.",
                ),
            )
        )
        first = json.loads(
            await service._call_managed_agent(
                mode="consult",
                message="先讨论",
                agent_id="media-writer-agent",
                instance_id="",
                shared_context="脚本上下文",
                primary_profile=primary,
                primary_session_id="main-session",
                user_id="main-user",
                model=None,
                task=None,
                available_workspace_tools=workspace_tools,
                runtime_context=runtime_context,
            )
        )
        second = json.loads(
            await service._call_managed_agent(
                mode="delegate",
                message="现在执行",
                agent_id="",
                instance_id=first["instance_id"],
                shared_context="",
                primary_profile=primary,
                primary_session_id="main-session",
                user_id="main-user",
                model=None,
                task=None,
                available_workspace_tools=workspace_tools,
                runtime_context=runtime_context,
            )
        )

        assert second["instance_id"] == first["instance_id"]
        assert calls[0]["tools"] == ["read_script_workspace"]
        assert calls[0]["extra_tools"] == []
        assert calls[0]["skill_package"] == "media-writer-consult-package"
        assert calls[0]["message"].endswith(
            "Context explicitly shared by the MainAgent:\n脚本上下文"
        )
        assert calls[1]["thread_id"] == "child-thread-1"
        assert calls[1]["tools"] == [
            "read_script_workspace",
            "write_script_workspace",
        ]
        assert calls[1]["extra_tools"] == ["media_master_library"]
        assert calls[1]["skill_package"] == "media-writer-delegate-package"
        assert "Prefer a direct opening" in calls[0]["runtime_prompt"]
        assert "Prefer a direct opening" in calls[1]["runtime_prompt"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_resolve_managed_agent_prefers_matching_real_instance_id(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'managed-resolve.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            primary = await get_agent_profile(session, "default-single-agent")
        assert primary is not None

        service = MainAgentService(_options(tmp_path))
        managed = await service.managed_store.create(
            primary_session_id="main-session",
            primary_agent_id=primary.agent_id,
            agent_id="media-writer-agent",
            user_id="main-user",
        )

        resolved = await service._resolve_managed_agent(
            mode="delegate",
            agent_id="media-writer-agent",
            instance_id=managed.instance_id,
            primary_profile=primary,
            primary_session_id="main-session",
            user_id="main-user",
        )

        assert resolved.instance_id == managed.instance_id
        rows = await service.managed_store.list(
            primary_session_id="main-session",
            user_id="main-user",
        )
        assert len(rows) == 1

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_resolve_managed_agent_rejects_fake_or_mismatched_instance_id(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'managed-invalid.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            primary = await get_agent_profile(session, "default-single-agent")
        assert primary is not None

        service = MainAgentService(_options(tmp_path))
        managed = await service.managed_store.create(
            primary_session_id="main-session",
            primary_agent_id=primary.agent_id,
            agent_id="media-writer-agent",
            user_id="main-user",
        )

        with pytest.raises(ValueError, match="not found"):
            await service._resolve_managed_agent(
                mode="delegate",
                agent_id="media-writer-agent",
                instance_id="instance_writer",
                primary_profile=primary,
                primary_session_id="main-session",
                user_id="main-user",
            )

        with pytest.raises(ValueError, match="belongs to 'media-writer-agent'"):
            await service._resolve_managed_agent(
                mode="delegate",
                agent_id="media-storyboard-agent",
                instance_id=managed.instance_id,
                primary_profile=primary,
                primary_session_id="main-session",
                user_id="main-user",
            )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_managed_agent_stream_wraps_single_events_and_requires_write(tmp_path, monkeypatch):
    async def fake_child_stream(self, profile, request):
        thread_id = request.thread_id or "child-stream-thread"
        yield SingleAgentStreamEvent(
            event="metadata",
            thread_id=thread_id,
            session_id=request.session_id,
        )
        yield SingleAgentStreamEvent(
            event="chunk",
            thread_id=thread_id,
            session_id=request.session_id,
            data={"choices": [{"delta": {"content": "writing"}}]},
        )
        yield SingleAgentStreamEvent(
            event="chunk",
            thread_id=thread_id,
            session_id=request.session_id,
            data={
                "observation": {
                    "tool": {"id": "write-1", "function": {"name": "write_script_workspace"}},
                    "result": '{"status":"saved"}',
                    "error": None,
                }
            },
        )
        yield SingleAgentStreamEvent(
            event="final",
            thread_id=thread_id,
            session_id=request.session_id,
            data={"content": "writing saved", "usage": {"total_tokens": 12}},
        )

    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'managed-stream.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(_ScopedSingleAgentService, "stream_chat", fake_child_stream)
        await init_db()
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            primary = await get_agent_profile(session, "default-single-agent")
        assert primary is not None

        async def read_demo() -> str:
            return "workspace"

        async def write_demo(script_text: str) -> str:
            return script_text

        read_tool = FunctionTool.from_defaults(
            async_fn=read_demo,
            name="read_script_workspace",
            description="test tool",
        )
        write_tool = FunctionTool.from_defaults(
            async_fn=write_demo,
            name="write_script_workspace",
            description="test tool",
            return_direct=True,
        )
        events = []

        async def emit(item):
            events.append(item)

        result = json.loads(
            await MainAgentService(_options(tmp_path))._call_managed_agent(
                mode="delegate",
                message="write",
                agent_id="media-writer-agent",
                instance_id="",
                shared_context="",
                primary_profile=primary,
                primary_session_id="main-stream-session",
                user_id="main-user",
                model=None,
                task=None,
                available_workspace_tools=[read_tool, write_tool],
                event_sink=emit,
            )
        )

        assert result["response"] == "writing saved"
        assert [item["event"] for item in events] == [
            "turn_started",
            "metadata",
            "chunk",
            "chunk",
            "final",
            "turn_finished",
        ]
        started = events[0]["data"]
        assert started["role"] == "subagent"
        assert started["agent_id"] == "media-writer-agent"
        assert all(
            item["data"]["turn_id"] == started["turn_id"]
            for item in events
        )
        wrapped = events[3]["data"]["single_event"]
        assert wrapped["data"]["observation"]["tool"]["function"]["name"] == "write_script_workspace"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_main_agent_stream_merges_subagent_events_and_augments_final(tmp_path, monkeypatch):
    async def fake_prepare(self, request, *, stream, event_sink=None):
        self._test_event_sink = event_sink
        return {
            "request": request,
            "primary_session_id": "main-session",
            "scoped_request": request.model_copy(update={"stream": True}),
            "profile": _main_runtime_profile(),
            "workspace": None,
            "task": None,
        }

    async def fake_single_stream(self, profile, request):
        yield SingleAgentStreamEvent(
            event="metadata",
            thread_id="main-thread",
            session_id="main-session",
        )
        await self._test_event_sink(
            {
                "event": "turn_started",
                "data": {
                    "turn_id": "child-turn",
                    "role": "subagent",
                    "instance_id": "child-instance",
                    "agent_id": "media-writer-agent",
                    "name": "Media Writer Agent",
                    "mode": "delegate",
                },
            }
        )
        yield SingleAgentStreamEvent(
            event="chunk",
            thread_id="main-thread",
            session_id="main-session",
            data={"choices": [{"delta": {"content": "main"}}]},
        )
        yield SingleAgentStreamEvent(
            event="final",
            thread_id="main-thread",
            session_id="main-session",
            data={"content": "main done", "usage": {"total_tokens": 4}},
        )

    async def fake_finalize(self, prepared, *, thread_id, session_id, response_content):
        return {
            "workspace": {"id": "workspace-1", "script_text": "script", "storyboard_text": ""},
            "managed_agents": [],
            "main_session": {"session_id": session_id, "thread_id": thread_id, "phase": "active"},
            "task": {"id": "task-1", "status": "succeeded"},
        }

    async def run():
        monkeypatch.setattr(MainAgentService, "_prepare_turn", fake_prepare)
        monkeypatch.setattr(SchedulingService, "stream_chat", fake_single_stream)
        monkeypatch.setattr(MainAgentService, "_finalize_turn", fake_finalize)
        events = [
            item
            async for item in MainAgentService(_options(tmp_path)).stream_chat(
                MainAgentChatRequest(message="stream", session_id="main-session", stream=True)
            )
        ]

        assert [item["event"] for item in events] == [
            "metadata",
            "turn_started",
            "chunk",
            "final",
        ]
        final = events[-1]["data"]
        assert final["data"]["content"] == "main done"
        assert final["data"]["workspace"]["script_text"] == "script"
        assert final["data"]["task"]["status"] == "succeeded"

    asyncio.run(run())


def test_script_task_can_be_completed_by_external_main_agent(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'task.db'}")
        reset_engine_for_test()
        await init_db()
        workspace = await ScriptWorkspaceStore().create(script_text="v1", user_id="main-user")
        task_service = TaskManagerService(_options(tmp_path))
        task = await task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.generate",
                task_key="media_script",
                input_payload={
                    "workspace_id": workspace.id,
                    "instruction": "改成 v2",
                    "operation": "interact",
                },
                user_id="main-user",
                stream=False,
            )
        )
        task = await task_service.begin_external_task(task.id, user_id="main-user")
        assert task.status == "running"
        task = await task_service.complete_external_task(
            task.id,
            result={
                "workspace_id": workspace.id,
                "script_text": "v2",
                "storyboard_text": "",
                "response": "saved",
            },
        )
        assert task.status == "succeeded"
        assert task.result_payload_json["script_text"] == "v2"
        events = await task_service.list_events(task.id)
        assert [event.event_type for event in events] == [
            "task_created",
            "task_started",
            "task_succeeded",
        ]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
