import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from llama_index.core.tools.function_tool import FunctionTool

from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.api import create_scheduling_router
from scheduling.main_agent.service import (
    MainAgentService,
    _ScopedSingleAgentService,
    _main_runtime_profile,
    _require_managed_write,
    _tools_allowed_for_managed_agent,
)
from scheduling.main_agent.schemas import MainAgentChatRequest
from scheduling.main_agent.store import (
    MainAgentSessionStore,
    ManagedSingleAgentStore,
    ScriptWorkspaceStore,
)
from scheduling.scheduler import SchedulingRuntimeOptions, SchedulingService
from service.agent import SingleAgentStreamEvent
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

    with pytest.raises(ValueError, match="completed without a successful"):
        _require_managed_write(
            "media-writer-agent",
            "delegate",
            [next(tool for tool in tools if tool.metadata.name == "write_script_workspace")],
            set(),
        )


def test_workspace_write_tools_are_terminal_and_return_small_receipts(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'workspace-tools.db'}")
        reset_engine_for_test()
        await init_db()
        workspace = await ScriptWorkspaceStore().create(script_text="v1", user_id="main-user")
        task_service = TaskManagerService(_options(tmp_path))
        task = await task_service.create_task(
            TaskCreateRequest(
                task_type="media.script.text.modify",
                input_payload={"workspace_id": workspace.id, "instruction": "write"},
                user_id="main-user",
                agent_id="main-agent-runtime",
                stream=False,
            )
        )
        task = await task_service.begin_external_task(task.id, user_id="main-user")
        tools = MainAgentService(_options(tmp_path))._workspace_tools(workspace.id, task)
        by_name = {tool.metadata.name: tool for tool in tools}

        assert by_name["read_script_workspace"].metadata.return_direct is False
        assert by_name["write_script_workspace"].metadata.return_direct is True
        assert by_name["write_storyboard_workspace"].metadata.return_direct is True

        output = await by_name["write_script_workspace"].acall(script_text="final script")
        receipt = json.loads(output.content)
        assert receipt == {
            "status": "saved",
            "workspace_id": workspace.id,
            "field": "script_text",
            "character_count": len("final script"),
        }
        assert "final script" not in output.content

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


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
                "prompt": self.runtime_prompt,
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

        async def write_demo(script_text: str) -> str:
            return script_text

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
                runtime_tools=[write_tool],
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
