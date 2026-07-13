import asyncio
import json

import httpx
from fastapi import FastAPI

from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.api import create_scheduling_router
from scheduling.main_agent.service import (
    MainAgentService,
    _ScopedSingleAgentService,
    _main_runtime_profile,
    _tools_allowed_for_managed_agent,
)
from scheduling.main_agent.store import (
    MainAgentSessionStore,
    ManagedSingleAgentStore,
    ScriptWorkspaceStore,
)
from scheduling.scheduler import SchedulingRuntimeOptions, SchedulingService
from task_manager.schemas import TaskCreateRequest
from task_manager.service import TaskManagerService
from db.db_context import create_db_session, init_db, reset_engine_for_test


def _options(tmp_path):
    return SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")


def test_media_specialists_keep_shared_reads_but_have_separate_write_tools():
    from llama_index.core.tools.function_tool import FunctionTool

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

    writer_names = [
        tool.metadata.name
        for tool in _tools_allowed_for_managed_agent("media-writer-agent", tools)
    ]
    storyboard_names = [
        tool.metadata.name
        for tool in _tools_allowed_for_managed_agent("media-storyboard-agent", tools)
    ]
    assert writer_names == ["read_script_workspace", "write_script_workspace"]
    assert storyboard_names == ["read_script_workspace", "write_storyboard_workspace"]


def test_main_agent_reuses_single_scheduler_and_persists_only_its_session_state(tmp_path, monkeypatch):
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
            main_session=main_session,
        )
        assert [tool.metadata.name for tool in new_tools] == [
            "produce_script_and_storyboard",
            "consult_agent",
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
            main_session=main_session,
        )
        assert [tool.metadata.name for tool in active_tools] == [
            "consult_agent",
            "delegate_agent",
            "list_active_agents",
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


def test_main_agent_routes_expose_catalog_and_workspace(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
        reset_engine_for_test()
        await init_db()
        app = FastAPI()
        app.include_router(create_scheduling_router(_options(tmp_path)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            catalog = await client.get("/scheduling/agents/delegatable")
            assert catalog.status_code == 200
            assert any(
                row["agent_id"] == "media-writer-agent"
                for row in catalog.json()["agents"]
            )
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
                "prompt": self.runtime_prompt,
            }
        )
        return {
            "thread_id": request.thread_id or "child-thread-1",
            "session_id": request.session_id,
            "response": {"choices": [{"message": {"content": f"reply-{len(calls)}"}}]},
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
                runtime_tools=[],
            )
        )
        from llama_index.core.tools.function_tool import FunctionTool

        async def write_demo(value: str) -> str:
            return value

        write_tool = FunctionTool.from_defaults(
            async_fn=write_demo,
            name="write_script_workspace",
            description="test tool",
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
                runtime_tools=[write_tool],
            )
        )

        assert second["instance_id"] == first["instance_id"]
        assert calls[0]["tools"] == []
        assert "consultation only" in calls[0]["prompt"]
        assert calls[1]["thread_id"] == "child-thread-1"
        assert calls[1]["tools"] == ["write_script_workspace"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_script_modify_task_can_be_completed_by_external_main_agent(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'task.db'}")
        reset_engine_for_test()
        await init_db()
        workspace = await ScriptWorkspaceStore().create(script_text="v1", user_id="main-user")
        task_service = TaskManagerService(_options(tmp_path))
        task = await task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.text.modify",
                input_payload={"workspace_id": workspace.id, "instruction": "改成 v2"},
                user_id="main-user",
                agent_id="default-single-agent",
                stream=False,
            )
        )
        task = await task_service.begin_external_task(task.id, user_id="main-user")
        assert task.status == "running"
        task = await task_service.complete_external_task(
            task.id,
            result={"workspace_id": workspace.id, "script_text": "v2"},
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
