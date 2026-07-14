from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from llama_index.core.tools.function_tool import FunctionTool
from scheduling.agent_registry import (
    ensure_default_agent_profiles,
    get_agent_profile,
    list_agent_profiles,
)
from scheduling.agent_registry.models import AgentProfileEntity
from scheduling.scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService
from scheduling.scheduler.service import SchedulingToolContext
from task_manager.schemas import TaskCreateRequest
from task_manager.service import TaskManagerService, task_to_read

from .models import MainAgentSessionEntity, ManagedSingleAgentEntity
from .schemas import MainAgentChatRequest
from .store import MainAgentSessionStore, ManagedSingleAgentStore, ScriptWorkspaceStore


MAIN_RUNTIME_AGENT_ID = "main-agent-runtime"
MAIN_RUNTIME_PROMPT = (
    "You are Media Main Agent. You own the user conversation and coordinate specialist Single Agents. "
    "Keep the shared Workspace as the source of truth, never pretend that chat text was saved, and never "
    "write production content directly when a specialist workflow is required."
)

SubagentEventSink = Callable[[dict[str, Any]], Awaitable[None]]


def _main_runtime_profile(model_id: str | None = None) -> AgentProfileEntity:
    """Build the primary runtime config without registering it as a reusable Agent."""
    return AgentProfileEntity(
        agent_id=MAIN_RUNTIME_AGENT_ID,
        name="Media Main Agent",
        description="Main scheduling runtime for media production.",
        model_id=model_id or "deepseek-v4-pro",
        system_prompt=MAIN_RUNTIME_PROMPT,
        default_tools_json="[]",
        default_datasets_json="[]",
        runtime_config_json="{}",
    )


def _assistant_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


class _ScopedSingleAgentService(SchedulingService):
    """Single scheduling with additions owned only by the MainAgent module."""

    def __init__(
        self,
        options: SchedulingRuntimeOptions,
        *,
        runtime_tools: list[FunctionTool] | None = None,
        runtime_prompt: str = "",
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        super().__init__(options, tenant_id=tenant_id)
        self.runtime_tools = list(runtime_tools or [])
        self.runtime_prompt = runtime_prompt

    async def _build_context(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
    ) -> SchedulingToolContext:
        context = await super()._build_context(profile, request)
        context.tools.extend(self.runtime_tools)
        if self.runtime_prompt:
            context.task_prompt = "\n\n".join(
                item for item in [context.task_prompt, self.runtime_prompt] if item
            )
        return context


class MainAgentService(_ScopedSingleAgentService):
    """A Single Agent enhanced with managed Single Agent delegation tools."""

    def __init__(
        self,
        options: SchedulingRuntimeOptions,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        super().__init__(options, tenant_id=tenant_id)
        self.session_store = MainAgentSessionStore()
        self.managed_store = ManagedSingleAgentStore()
        self.workspace_store = ScriptWorkspaceStore()
        self.task_service = TaskManagerService(options)
        self.subagent_outputs: list[dict[str, str]] = []

    async def chat(
        self,
        request: MainAgentChatRequest,
    ) -> dict[str, Any]:
        if request.stream:
            raise ValueError("Use stream_chat for a streaming MainAgent request.")

        prepared = await self._prepare_turn(request, stream=False)
        try:
            body = await super().chat(prepared["profile"], prepared["scoped_request"])
            body.update(
                await self._finalize_turn(
                    prepared,
                    thread_id=body["thread_id"],
                    session_id=body["session_id"],
                    response_content=_assistant_content(body.get("response") or {}),
                )
            )
            return body
        except Exception as exc:
            await self._fail_prepared_task(prepared, exc)
            raise

    async def stream_chat(
        self,
        request: MainAgentChatRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        sentinel = object()
        prepared: dict[str, Any] | None = None

        async def emit(item: dict[str, Any]) -> None:
            await queue.put(item)

        async def produce() -> None:
            nonlocal prepared
            try:
                prepared = await self._prepare_turn(request, stream=True, event_sink=emit)
                final_event = None
                async for event in super(MainAgentService, self).stream_chat(
                    prepared["profile"], prepared["scoped_request"]
                ):
                    if event.event == "final":
                        final_event = event
                        continue
                    await emit(
                        {
                            "event": event.event,
                            "data": event.model_dump(mode="json", exclude_none=True),
                        }
                    )

                if final_event is None:
                    raise RuntimeError("MainAgent stream ended without a final event.")
                final_data = final_event.data or {}
                extras = await self._finalize_turn(
                    prepared,
                    thread_id=final_event.thread_id,
                    session_id=final_event.session_id,
                    response_content=str(final_data.get("content") or ""),
                )
                final_event.data = {**final_data, **extras}
                await emit(
                    {
                        "event": "final",
                        "data": final_event.model_dump(mode="json", exclude_none=True),
                    }
                )
            except asyncio.CancelledError:
                if prepared is not None:
                    await self._fail_prepared_task(
                        prepared, RuntimeError("MainAgent stream was cancelled.")
                    )
                raise
            except Exception as exc:
                if prepared is not None:
                    await self._fail_prepared_task(prepared, exc)
                await emit({"event": "error", "data": {"message": str(exc)}})
            finally:
                await queue.put(sentinel)

        producer = asyncio.create_task(produce())
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                yield item  # type: ignore[misc]
            await producer
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

    async def _prepare_turn(
        self,
        request: MainAgentChatRequest,
        *,
        stream: bool,
        event_sink: SubagentEventSink | None = None,
    ) -> dict[str, Any]:
        self.subagent_outputs = []
        primary_session_id = request.session_id or request.thread_id or f"main-agent:{uuid.uuid4().hex}"
        scoped_request = request.model_copy(
            update={"session_id": primary_session_id, "stream": stream}
        )
        main_session = await self.session_store.get_or_create(
            primary_session_id,
            user_id=request.user_id,
            tenant_id=self.tenant_id,
        )
        profile = _main_runtime_profile(request.model)
        workspace = None
        task = None
        if request.workspace_id:
            workspace = await self.workspace_store.get(
                request.workspace_id,
                user_id=request.user_id,
                tenant_id=self.tenant_id,
            )
            if workspace is None:
                raise ValueError(f"Script workspace '{request.workspace_id}' not found.")
            instruction = request.message or _last_user_message(request.messages)
            task = await self.task_service.create_task(
                TaskCreateRequest(
                    task_type="media.script.text.modify",
                    title="MainAgent script workspace turn",
                    input_payload={"workspace_id": workspace.id, "instruction": instruction},
                    user_id=request.user_id,
                    tenant_id=self.tenant_id,
                    stream=stream,
                    agent_id=MAIN_RUNTIME_AGENT_ID,
                    thread_id=request.thread_id,
                    session_id=primary_session_id,
                )
            )
            task = await self.task_service.begin_external_task(task.id, user_id=request.user_id)

        prepared = {
            "request": request,
            "primary_session_id": primary_session_id,
            "scoped_request": scoped_request,
            "main_session": main_session,
            "profile": profile,
            "workspace": workspace,
            "task": task,
        }
        try:
            catalog = await self.list_delegatable_agents()
            managed = await self.managed_store.list(
                primary_session_id=primary_session_id,
                user_id=request.user_id,
                tenant_id=self.tenant_id,
            )
            workspace_tools = self._workspace_tools(workspace.id, task) if workspace else []
            delegation_tools = self._delegation_tools(
                primary_profile=profile,
                primary_session_id=primary_session_id,
                user_id=request.user_id,
                model=request.model,
                task=task,
                workspace_tools=workspace_tools,
                main_session=main_session,
                event_sink=event_sink,
            )
            main_workspace_tools = [
                tool for tool in workspace_tools if tool.metadata.name == "read_script_workspace"
            ]
            self.runtime_tools = delegation_tools + main_workspace_tools
            self.runtime_prompt = _main_agent_prompt(
                catalog, managed, workspace, phase=main_session.phase
            )
            return prepared
        except Exception as exc:
            await self._fail_prepared_task(prepared, exc)
            raise

    async def _finalize_turn(
        self,
        prepared: dict[str, Any],
        *,
        thread_id: str,
        session_id: str,
        response_content: str,
    ) -> dict[str, Any]:
        request = prepared["request"]
        primary_session_id = prepared["primary_session_id"]
        workspace = prepared["workspace"]
        task = prepared["task"]
        await self.managed_store.bind_primary_thread(
            primary_session_id,
            thread_id,
            user_id=request.user_id,
            tenant_id=self.tenant_id,
        )
        main_session = await self.session_store.update(
            primary_session_id,
            user_id=request.user_id,
            tenant_id=self.tenant_id,
            thread_id=thread_id,
        )
        managed = await self.managed_store.list(
            primary_session_id=primary_session_id,
            user_id=request.user_id,
            tenant_id=self.tenant_id,
        )
        extras: dict[str, Any] = {
            "managed_agents": [row.to_read_model() for row in managed],
            "subagent_outputs": list(self.subagent_outputs),
            "main_session": main_session.to_read_model(),
        }
        if workspace:
            workspace = await self.workspace_store.get(
                workspace.id,
                user_id=request.user_id,
                tenant_id=self.tenant_id,
            )
            extras["workspace"] = workspace.to_read_model() if workspace else None
        if task:
            result = {
                "workspace_id": workspace.id if workspace else request.workspace_id,
                "script_text": workspace.script_text if workspace else "",
                "storyboard_text": workspace.storyboard_text if workspace else "",
                "response": response_content,
            }
            task = await self.task_service.complete_external_task(
                task.id,
                result=result,
                thread_id=thread_id,
                session_id=session_id,
            )
            extras["task"] = task_to_read(task).model_dump(mode="json")
        return extras

    async def _fail_prepared_task(
        self,
        prepared: dict[str, Any],
        exc: Exception,
    ) -> None:
        task = prepared.get("task")
        if task is not None:
            await self.task_service.fail_external_task(task.id, exc)

    async def list_delegatable_agents(self, *, exclude_agent_id: str | None = None) -> list[dict[str, str]]:
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profiles = await list_agent_profiles(session)
        rows = []
        for profile in profiles:
            if profile.agent_type != "single" or profile.agent_id == exclude_agent_id:
                continue
            delegation = profile.runtime_config.get("delegation") or {}
            rows.append(
                {
                    "agent_id": profile.agent_id,
                    "name": profile.name,
                    "good_at": profile.description,
                    "use_when": str(delegation.get("use_when") or profile.description),
                }
            )
        return rows

    def _workspace_tools(self, workspace_id: str, task) -> list[FunctionTool]:
        async def read_script_workspace() -> str:
            row = await self.workspace_store.get(
                workspace_id,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
            )
            if row is None:
                raise ValueError(f"Script workspace '{workspace_id}' not found.")
            return json.dumps(
                {
                    "workspace_id": row.id,
                    "script_text": row.script_text,
                    "storyboard_text": row.storyboard_text,
                },
                ensure_ascii=False,
            )

        async def write_script_workspace(script_text: str) -> str:
            row = await self.workspace_store.update(
                workspace_id,
                script_text=script_text,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
            )
            await self.task_service.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="workspace_updated",
                stage="main_agent",
                message="Script workspace updated.",
                payload={"workspace_id": workspace_id, "character_count": len(script_text)},
                source={"type": "workspace", "id": workspace_id},
            )
            return json.dumps(
                {
                    "status": "saved",
                    "workspace_id": row.id,
                    "field": "script_text",
                    "character_count": len(row.script_text),
                },
                ensure_ascii=False,
            )

        async def write_storyboard_workspace(storyboard_text: str) -> str:
            row = await self.workspace_store.update(
                workspace_id,
                storyboard_text=storyboard_text,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
            )
            await self.task_service.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="workspace_updated",
                stage="main_agent",
                message="Storyboard workspace updated.",
                payload={"workspace_id": workspace_id, "character_count": len(storyboard_text)},
                source={"type": "workspace", "id": workspace_id},
            )
            return json.dumps(
                {
                    "status": "saved",
                    "workspace_id": row.id,
                    "field": "storyboard_text",
                    "character_count": len(row.storyboard_text),
                },
                ensure_ascii=False,
            )

        return [
            FunctionTool.from_defaults(
                async_fn=read_script_workspace,
                name="read_script_workspace",
                description="Read the latest shared script and storyboard text for the current task.",
            ),
            FunctionTool.from_defaults(
                async_fn=write_script_workspace,
                name="write_script_workspace",
                description="Save the complete shared script. A successful save completes this agent turn.",
                return_direct=True,
            ),
            FunctionTool.from_defaults(
                async_fn=write_storyboard_workspace,
                name="write_storyboard_workspace",
                description="Save the complete shared storyboard. A successful save completes this agent turn.",
                return_direct=True,
            ),
        ]

    def _delegation_tools(
        self,
        *,
        primary_profile: AgentProfileEntity,
        primary_session_id: str,
        user_id: str,
        model: str | None,
        task,
        workspace_tools: list[FunctionTool],
        main_session: MainAgentSessionEntity,
        event_sink: SubagentEventSink | None = None,
    ) -> list[FunctionTool]:
        async def consult_agent(
            message: str,
            agent_id: str = "",
            instance_id: str = "",
            shared_context: str = "",
        ) -> str:
            return await self._call_managed_agent(
                mode="consult",
                message=message,
                agent_id=agent_id,
                instance_id=instance_id,
                shared_context=shared_context,
                primary_profile=primary_profile,
                primary_session_id=primary_session_id,
                user_id=user_id,
                model=model,
                task=task,
                runtime_tools=[],
                event_sink=event_sink,
            )

        async def delegate_agent(
            instruction: str,
            agent_id: str = "",
            instance_id: str = "",
            shared_context: str = "",
        ) -> str:
            return await self._call_managed_agent(
                mode="delegate",
                message=instruction,
                agent_id=agent_id,
                instance_id=instance_id,
                shared_context=shared_context,
                primary_profile=primary_profile,
                primary_session_id=primary_session_id,
                user_id=user_id,
                model=model,
                task=task,
                runtime_tools=workspace_tools,
                event_sink=event_sink,
            )

        async def list_active_agents() -> str:
            rows = await self.managed_store.list(
                primary_session_id=primary_session_id,
                user_id=user_id,
                tenant_id=self.tenant_id,
            )
            return json.dumps([row.to_read_model() for row in rows], ensure_ascii=False)

        async def produce_script_and_storyboard(instruction: str) -> str:
            if task is None or not workspace_tools:
                raise ValueError("Create a Workspace before starting media production.")
            current_session = await self.session_store.get_or_create(
                primary_session_id,
                user_id=user_id,
                tenant_id=self.tenant_id,
            )
            if current_session.phase != "new":
                raise ValueError(
                    "The initial production workflow is complete; use delegate_agent for later revisions."
                )

            managed = await self.managed_store.list(
                primary_session_id=primary_session_id,
                user_id=user_id,
                tenant_id=self.tenant_id,
            )
            for target_agent_id, child_instruction in [
                (
                    "media-writer-agent",
                    f"{instruction}\n请读取共享 Workspace，完成脚本修改并保存完整脚本。",
                ),
                (
                    "media-storyboard-agent",
                    f"{instruction}\n请读取脚本 Agent 刚保存的最新脚本，生成并保存完整分镜。",
                ),
            ]:
                existing = next((row for row in managed if row.agent_id == target_agent_id), None)
                await self._call_managed_agent(
                    mode="delegate",
                    message=child_instruction,
                    agent_id="" if existing else target_agent_id,
                    instance_id=existing.instance_id if existing else "",
                    shared_context="",
                    primary_profile=primary_profile,
                    primary_session_id=primary_session_id,
                    user_id=user_id,
                    model=model,
                    task=task,
                    runtime_tools=workspace_tools,
                    event_sink=event_sink,
                )
            await self.session_store.update(
                primary_session_id,
                user_id=user_id,
                tenant_id=self.tenant_id,
                phase="active",
            )
            workspace = await self.workspace_store.get(
                task.input_payload_json["workspace_id"],
                user_id=user_id,
                tenant_id=self.tenant_id,
            )
            return json.dumps(
                {
                    "status": "completed",
                    "specialists": self.subagent_outputs[-2:],
                    "workspace": workspace.to_read_model() if workspace else None,
                },
                ensure_ascii=False,
            )

        tools = [
            FunctionTool.from_defaults(
                async_fn=consult_agent,
                name="consult_agent",
                description="Ask a managed Single Agent for advice. Reuse instance_id for follow-up discussion.",
            ),
            FunctionTool.from_defaults(
                async_fn=delegate_agent,
                name="delegate_agent",
                description="Delegate execution to a managed Single Agent. It may receive current task tools.",
            ),
            FunctionTool.from_defaults(
                async_fn=list_active_agents,
                name="list_active_agents",
                description="List Single Agent instances already managed by this MainAgent conversation.",
            ),
        ]
        if main_session.phase == "new":
            tools.insert(
                0,
                FunctionTool.from_defaults(
                    async_fn=produce_script_and_storyboard,
                    name="produce_script_and_storyboard",
                    description=(
                        "Run the mandatory first media-production workflow: the writer saves the complete script, "
                        "then the storyboard specialist reads that saved script and saves the complete storyboard. "
                        "Use this for the first request to create or revise production content."
                    ),
                ),
            )
            tools = [tool for tool in tools if tool.metadata.name != "delegate_agent"]
        return tools

    async def _call_managed_agent(
        self,
        *,
        mode: str,
        message: str,
        agent_id: str,
        instance_id: str,
        shared_context: str,
        primary_profile: AgentProfileEntity,
        primary_session_id: str,
        user_id: str,
        model: str | None,
        task,
        runtime_tools: list[FunctionTool],
        event_sink: SubagentEventSink | None = None,
    ) -> str:
        managed = await self._resolve_managed_agent(
            agent_id=agent_id,
            instance_id=instance_id,
            primary_profile=primary_profile,
            primary_session_id=primary_session_id,
            user_id=user_id,
        )
        async with create_db_session() as session:
            child_profile = await get_agent_profile(session, managed.agent_id)
        if child_profile is None or not child_profile.enabled or child_profile.agent_type != "single":
            raise ValueError(f"Agent '{managed.agent_id}' is not an enabled Single Agent.")

        if task is not None:
            await self.task_service.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type=f"subagent_{mode}_started",
                stage="main_agent",
                message=f"{mode.title()} call started for {managed.agent_id}.",
                payload={"instance_id": managed.instance_id},
                agent_id=managed.agent_id,
                source={"type": "managed_agent", "id": managed.instance_id},
            )

        mode_prompt = _managed_agent_prompt(managed.agent_id, mode)
        if shared_context:
            mode_prompt += f"\n\nContext explicitly shared by the MainAgent:\n{shared_context}"
        runtime_tools = _tools_allowed_for_managed_agent(managed.agent_id, runtime_tools)
        child_service = _ScopedSingleAgentService(
            self.options,
            runtime_tools=runtime_tools,
            runtime_prompt=mode_prompt,
            tenant_id=self.tenant_id,
        )
        child_request = SchedulingChatRequest(
            message=message,
            model=model,
            thread_id=managed.child_thread_id,
            session_id=managed.child_session_id,
            user_id=user_id,
            stream=event_sink is not None,
        )
        successful_tools: set[str] = set()
        if event_sink is None:
            child_result = await child_service.chat(child_profile, child_request)
            child_thread_id = child_result["thread_id"]
            content = _assistant_content(child_result.get("response") or {})
            successful_tools = _successful_tool_names(
                (child_result.get("response") or {}).get("steps") or []
            )
        else:
            turn_id = uuid.uuid4().hex
            event_base = {
                "turn_id": turn_id,
                "role": "subagent",
                "instance_id": managed.instance_id,
                "agent_id": managed.agent_id,
                "name": child_profile.name,
                "mode": mode,
            }
            await event_sink({"event": "turn_started", "data": event_base})
            content_parts: list[str] = []
            content = ""
            child_thread_id = managed.child_thread_id or ""
            try:
                async for event in child_service.stream_chat(child_profile, child_request):
                    child_thread_id = event.thread_id or child_thread_id
                    data = event.data or {}
                    if event.event == "chunk":
                        choices = data.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            if delta.get("content"):
                                content_parts.append(str(delta["content"]))
                        observation = data.get("observation")
                        successful_tools.update(_successful_tool_names([observation] if observation else []))
                    elif event.event == "final":
                        content = str(data.get("content") or "".join(content_parts))
                    await event_sink(
                        {
                            "event": event.event,
                            "data": {**event_base, "single_event": event.model_dump(mode="json", exclude_none=True)},
                        }
                    )
                if not content:
                    content = "".join(content_parts)
            except Exception as exc:
                await event_sink(
                    {
                        "event": "turn_finished",
                        "data": {**event_base, "status": "error", "error": str(exc)},
                    }
                )
                raise

        _require_managed_write(managed.agent_id, mode, runtime_tools, successful_tools)
        await self.managed_store.update_child_thread(managed.instance_id, child_thread_id)
        self.subagent_outputs.append(
            {
                "mode": mode,
                "instance_id": managed.instance_id,
                "agent_id": managed.agent_id,
                "name": child_profile.name,
                "response": content,
            }
        )

        if event_sink is not None:
            await event_sink(
                {
                    "event": "turn_finished",
                    "data": {
                        **event_base,
                        "status": "completed",
                        "content": content,
                    },
                }
            )

        if task is not None:
            await self.task_service.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type=f"subagent_{mode}_completed",
                stage="main_agent",
                message=f"{mode.title()} call completed for {managed.agent_id}.",
                payload={"instance_id": managed.instance_id},
                agent_id=managed.agent_id,
                source={"type": "managed_agent", "id": managed.instance_id},
            )
        return json.dumps(
            {
                "instance_id": managed.instance_id,
                "agent_id": managed.agent_id,
                "response": content,
            },
            ensure_ascii=False,
        )

    async def _resolve_managed_agent(
        self,
        *,
        agent_id: str,
        instance_id: str,
        primary_profile: AgentProfileEntity,
        primary_session_id: str,
        user_id: str,
    ) -> ManagedSingleAgentEntity:
        if bool(agent_id) == bool(instance_id):
            raise ValueError("Provide exactly one of agent_id or instance_id.")
        if instance_id:
            row = await self.managed_store.get(
                instance_id,
                primary_session_id=primary_session_id,
                user_id=user_id,
                tenant_id=self.tenant_id,
            )
            if row is None:
                raise ValueError(f"Managed agent instance '{instance_id}' not found.")
            return row
        if agent_id == primary_profile.agent_id:
            raise ValueError("The MainAgent cannot manage itself as a child instance.")
        async with create_db_session() as session:
            profile = await get_agent_profile(session, agent_id)
        if profile is None or not profile.enabled or profile.agent_type != "single":
            raise ValueError(f"Agent '{agent_id}' is not an enabled Single Agent.")
        return await self.managed_store.create(
            primary_session_id=primary_session_id,
            primary_agent_id=primary_profile.agent_id,
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=self.tenant_id,
        )


def _last_user_message(messages: list[dict] | None) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _tools_allowed_for_managed_agent(
    agent_id: str,
    runtime_tools: list[FunctionTool],
) -> list[FunctionTool]:
    """Keep shared reads while separating the two media write capabilities."""
    write_tool_by_agent = {
        "media-writer-agent": "write_script_workspace",
        "media-storyboard-agent": "write_storyboard_workspace",
    }
    allowed_write_tool = write_tool_by_agent.get(agent_id)
    if allowed_write_tool is None:
        return runtime_tools
    return [
        tool
        for tool in runtime_tools
        if not tool.metadata.name.startswith("write_")
        or tool.metadata.name == allowed_write_tool
    ]


def _successful_tool_names(steps: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for step in steps or []:
        if not isinstance(step, dict) or step.get("error"):
            continue
        tool = step.get("tool") or {}
        function = tool.get("function") or {}
        name = function.get("name")
        if name:
            names.add(str(name))
    return names


def _require_managed_write(
    agent_id: str,
    mode: str,
    runtime_tools: list[FunctionTool],
    successful_tools: set[str],
) -> None:
    if mode != "delegate":
        return
    expected_by_agent = {
        "media-writer-agent": "write_script_workspace",
        "media-storyboard-agent": "write_storyboard_workspace",
    }
    expected = expected_by_agent.get(agent_id)
    available = {tool.metadata.name for tool in runtime_tools}
    if expected and expected in available and expected not in successful_tools:
        raise ValueError(f"Agent '{agent_id}' completed without a successful {expected} call.")


def _main_agent_prompt(
    catalog: list[dict[str, str]],
    managed: list[ManagedSingleAgentEntity],
    workspace,
    *,
    phase: str,
) -> str:
    sections = [
        "You are the MainAgent for a media production task. Decide whether to answer directly, consult a "
        "specialist, or delegate execution according to the user's current request. Reuse an existing instance_id "
        "when continuing with a managed agent so its conversation memory is preserved. Delegate only when a "
        "specialist should actually change shared work; use consult for discussion without writes.",
        "Delegatable Agent catalog:\n" + json.dumps(catalog, ensure_ascii=False),
        "Managed Agent instances:\n"
        + json.dumps([row.to_read_model() for row in managed], ensure_ascii=False),
    ]
    if phase == "new":
        sections.append(
            "This conversation has not completed its first production workflow. You may answer ordinary chat "
            "or questions about yourself directly. If the user asks to create or revise a script, storyboard, "
            "or other production content, you MUST call produce_script_and_storyboard exactly once and let it "
            "run writer first, then storyboard. Do not draft the production content yourself."
        )
    else:
        sections.append(
            "The first production workflow is complete. For later turns, decide whether to answer directly, "
            "consult a specialist, or delegate an edit. Prefer reusing an existing instance_id when the same "
            "specialist should continue with its earlier context."
        )
    if workspace:
        sections.append(
            "Current shared script text:\n"
            + (workspace.script_text or "(empty)")
            + "\n\nCurrent shared storyboard:\n"
            + (workspace.storyboard_text or "(empty)")
            + "\n\nWhen you delegate script work, require write_script_workspace. When you delegate storyboard "
            "work, require the agent to read the latest workspace and then use write_storyboard_workspace."
        )
    return "\n\n".join(sections)


def _managed_agent_prompt(agent_id: str, mode: str) -> str:
    if mode == "consult":
        return (
            "This turn is consultation only. Discuss the request and return concrete advice. "
            "Do not claim to modify shared data."
        )
    if agent_id == "media-writer-agent":
        return (
            "You own the script-writing step. First call read_script_workspace. Produce a complete replacement "
            "script, then call write_script_workspace with the full text. Know-how: open with a concrete 3-second "
            "hook; keep one clear audience and one central claim; use short speakable sentences; structure the body "
            "as hook, context, 2-4 evidence-backed points, transition, and closing action; preserve facts from the "
            "workspace; avoid unsupported numbers and generic slogans; include natural pauses and visual cues only "
            "when they help production. A successful Workspace write is the final result: do not review, revise, "
            "call another tool, or return a separate summary afterward."
        )
    if agent_id == "media-storyboard-agent":
        return (
            "You own the storyboard step. First call read_script_workspace so you use the writer's latest saved "
            "script. Create an executable shot list, then call write_storyboard_workspace with the full storyboard. "
            "Know-how: cover every narration segment; number shots; include time range, framing, subject/action, "
            "camera movement, matching voice-over, on-screen text, asset or location need, transition, and production "
            "notes; keep continuity of screen direction, wardrobe, props, light, and tempo; prefer shootable visuals "
            "over abstract descriptions. A successful Workspace write is the final result: do not review, revise, "
            "call another tool, or return a separate summary afterward."
        )
    return (
        "Execute the instruction. Read the current workspace before acting and use the available task write tool "
        "to save the completed result."
    )
