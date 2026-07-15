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
from task_manager.memory import TaskMemoryService, render_task_memory
from task_manager.service import TaskManagerService, task_to_read

from .models import ManagedSingleAgentEntity
from .schemas import MainAgentChatRequest, StoryboardShot, StoryboardWorkspacePayload
from .store import MainAgentSessionStore, ManagedSingleAgentStore, ScriptWorkspaceStore


MAIN_RUNTIME_AGENT_ID = "main-agent-runtime"
MAIN_SKILL_PACKAGE = "main-agent-orchestration-package"

SubagentEventSink = Callable[[dict[str, Any]], Awaitable[None]]


def _main_runtime_profile(model_id: str | None = None) -> AgentProfileEntity:
    """Build the primary runtime config without registering it as a reusable Agent."""
    return AgentProfileEntity(
        agent_id=MAIN_RUNTIME_AGENT_ID,
        name="Media Main Agent",
        description="Main scheduling runtime for media production.",
        model_id=model_id or "deepseek-v4-pro",
        system_prompt="You are Media Main Agent.",
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
        runtime_task_prompt: str = "",
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        super().__init__(options, tenant_id=tenant_id)
        self.runtime_tools = list(runtime_tools or [])
        self.runtime_task_prompt = runtime_task_prompt.strip()

    async def _build_context(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
    ) -> SchedulingToolContext:
        context = await super()._build_context(profile, request)
        context.tools.extend(self.runtime_tools)
        if self.runtime_task_prompt:
            context.task_prompt = "\n\n".join(
                item for item in [context.task_prompt, self.runtime_task_prompt] if item
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
        main_session = await self.session_store.get_or_create(
            primary_session_id,
            user_id=request.user_id,
            tenant_id=self.tenant_id,
        )
        profile = _main_runtime_profile(request.model)
        workspace = None
        task = None
        runtime_task_prompt = ""
        skill_package = MAIN_SKILL_PACKAGE
        if request.workspace_id:
            if not request.task_id:
                raise ValueError("task_id is required for a MainAgent Workspace request.")
            workspace = await self.workspace_store.get(
                request.workspace_id,
                user_id=request.user_id,
                tenant_id=self.tenant_id,
            )
            if workspace is None:
                raise ValueError(f"Script workspace '{request.workspace_id}' not found.")
            instruction = request.message or _last_user_message(request.messages)
            task = await self.task_service.get_task(request.task_id)
            _validate_formal_script_task(
                task,
                user_id=request.user_id,
                tenant_id=self.tenant_id,
                workspace_id=workspace.id,
                instruction=instruction,
            )
            skill_package = str(
                (task.definition_snapshot_json or {}).get("default_skill_package") or ""
            ).strip()
            if not skill_package:
                raise ValueError(f"Task '{task.id}' has no MainAgent Skill Package.")
            runtime_task_prompt = await self._build_task_runtime_prompt(task)
            task = await self.task_service.begin_external_task(
                task.id,
                user_id=request.user_id,
                stream=stream,
            )
        elif request.task_id:
            raise ValueError("workspace_id is required when task_id is provided.")

        scoped_request = request.model_copy(
            update={
                "session_id": primary_session_id,
                "stream": stream,
                "skill_package": skill_package,
            }
        )

        prepared = {
            "request": request,
            "primary_session_id": primary_session_id,
            "scoped_request": scoped_request,
            "main_session": main_session,
            "profile": profile,
            "workspace": workspace,
            "task": task,
            "runtime_task_prompt": runtime_task_prompt,
        }
        try:
            workspace_tools = self._workspace_tools(workspace.id, task) if workspace else []
            delegation_tools = self._delegation_tools(
                primary_profile=profile,
                primary_session_id=primary_session_id,
                user_id=request.user_id,
                model=request.model,
                task=task,
                workspace_tools=workspace_tools,
                runtime_task_prompt=runtime_task_prompt,
                event_sink=event_sink,
            )
            self.runtime_tools = delegation_tools + workspace_tools
            self.runtime_task_prompt = runtime_task_prompt
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
            operation = str((task.input_payload_json or {}).get("operation") or "interact")
            if operation == "generate":
                delegated_agents = [
                    item.get("agent_id")
                    for item in self.subagent_outputs
                    if item.get("mode") == "delegate"
                ]
                missing_agents = [
                    agent_id
                    for agent_id in ["media-writer-agent", "media-storyboard-agent"]
                    if agent_id not in delegated_agents
                ]
                if missing_agents:
                    raise ValueError(
                        "A generate task must delegate both Writer and Storyboard; missing: "
                        + ", ".join(missing_agents)
                    )
                if delegated_agents.index("media-writer-agent") > delegated_agents.index(
                    "media-storyboard-agent"
                ):
                    raise ValueError(
                        "A generate task must delegate Writer before Storyboard."
                    )
                if (
                    workspace is None
                    or not workspace.script_text.strip()
                    or not workspace.storyboard_text.strip()
                ):
                    raise ValueError(
                        "A generate task must save both script_text and storyboard_text before completion."
                    )
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

    async def _build_task_runtime_prompt(self, task) -> str:
        memory = None
        if task.task_key:
            memory = await TaskMemoryService(self.options).get_latest(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                task_key=task.task_key,
            )
        memory_prompt = render_task_memory(memory)
        task_prompt = "\n".join(
            [
                "# Formal Task Context",
                "This MainAgent turn executes the existing TaskManager task below.",
                f"Task ID: {task.id}",
                f"Task type: {task.task_type}",
                f"Stable task key: {task.task_key or '(none)'}",
                "Validated task input:",
                json.dumps(task.input_payload_json or {}, ensure_ascii=False, indent=2),
            ]
        )
        return "\n\n".join(item for item in [task_prompt, memory_prompt] if item)

    async def _fail_prepared_task(
        self,
        prepared: dict[str, Any],
        exc: Exception,
    ) -> None:
        task = prepared.get("task")
        if task is not None:
            await self.task_service.fail_external_task(task.id, exc)

    async def list_delegatable_agents(
        self,
        *,
        exclude_agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profiles = await list_agent_profiles(session)
        rows = []
        for profile in profiles:
            if profile.agent_type != "single" or profile.agent_id == exclude_agent_id:
                continue
            delegation = profile.runtime_config.get("delegation") or {}
            modes = delegation.get("modes") or {}
            if not delegation.get("enabled") or not isinstance(modes, dict) or not modes:
                continue
            rows.append(
                {
                    "agent_id": profile.agent_id,
                    "name": profile.name,
                    "good_at": profile.description,
                    "use_when": str(delegation.get("use_when") or profile.description),
                    "modes": {
                        str(mode): {
                            "skill_package": str((config or {}).get("skill_package") or ""),
                            "workspace_tools": [
                                str(name) for name in (config or {}).get("workspace_tools") or []
                            ],
                        }
                        for mode, config in modes.items()
                        if isinstance(config, dict)
                    },
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

        async def write_script_workspace(
            script_text: str,
            role_id: str = "",
            strategy_id: str = "",
            template_id: str = "",
            script_type_id: str = "",
            script_example_ids: list[str] | None = None,
            risk_rule_ids: list[str] | None = None,
            replace_reason: str = "",
        ) -> str:
            row = await self.workspace_store.update(
                workspace_id,
                script_text=script_text,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
            )
            master_library_usage = {
                "role_id": role_id,
                "strategy_id": strategy_id,
                "template_id": template_id,
                "script_type_id": script_type_id,
                "script_example_ids": script_example_ids or [],
                "risk_rule_ids": risk_rule_ids or [],
                "replace_reason": replace_reason,
            }
            if any(
                value
                for key, value in master_library_usage.items()
                if key not in {"script_example_ids", "risk_rule_ids"}
            ) or master_library_usage["script_example_ids"] or master_library_usage["risk_rule_ids"]:
                await self.task_service.update_task_metadata(
                    task.id,
                    {"master_library_usage": master_library_usage},
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

        async def write_storyboard_workspace(
            storyboard: list[StoryboardShot],
            storyboard_plan: dict[str, Any],
            visual_direction: list[str] | str,
            warnings: list[str],
        ) -> str:
            payload = StoryboardWorkspacePayload(
                storyboard=storyboard,
                storyboard_plan=storyboard_plan,
                visual_direction=visual_direction,
                warnings=warnings,
            )
            storyboard_text = json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
            )
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
                description=(
                    "Save the complete shared script and the ids of master-library cards actually adopted. "
                    "A successful save completes this agent turn."
                ),
                return_direct=True,
            ),
            FunctionTool.from_defaults(
                async_fn=write_storyboard_workspace,
                name="write_storyboard_workspace",
                description=(
                    "Save the complete shared storyboard as structured fields. "
                    "A successful save completes this agent turn."
                ),
                fn_schema=StoryboardWorkspacePayload,
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
        runtime_task_prompt: str = "",
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
                available_workspace_tools=workspace_tools,
                runtime_task_prompt=runtime_task_prompt,
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
                available_workspace_tools=workspace_tools,
                runtime_task_prompt=runtime_task_prompt,
                event_sink=event_sink,
            )

        async def list_delegatable_agents() -> str:
            rows = await self.list_delegatable_agents(
                exclude_agent_id=primary_profile.agent_id,
            )
            return json.dumps(rows, ensure_ascii=False)

        async def list_active_agents() -> str:
            rows = await self.managed_store.list(
                primary_session_id=primary_session_id,
                user_id=user_id,
                tenant_id=self.tenant_id,
            )
            return json.dumps([row.to_read_model() for row in rows], ensure_ascii=False)

        return [
            FunctionTool.from_defaults(
                async_fn=consult_agent,
                name="consult_agent",
                description=(
                    "Ask a managed Single Agent for read-only advice. Provide a complete consultation question "
                    "and reuse instance_id for follow-up discussion."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=delegate_agent,
                name="delegate_agent",
                description=(
                    "Delegate a complete execution instruction to a managed Single Agent. Its Skill Package and "
                    "Workspace permissions come from Agent Registry mode configuration."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=list_delegatable_agents,
                name="list_delegatable_agents",
                description="List reusable child Agent types and their supported consult/delegate modes.",
            ),
            FunctionTool.from_defaults(
                async_fn=list_active_agents,
                name="list_active_agents",
                description="List Single Agent instances already managed by this MainAgent conversation.",
            ),
        ]

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
        available_workspace_tools: list[FunctionTool],
        runtime_task_prompt: str = "",
        event_sink: SubagentEventSink | None = None,
    ) -> str:
        managed = await self._resolve_managed_agent(
            mode=mode,
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
        mode_config = _delegation_mode_config(child_profile, mode)
        runtime_tools = _select_workspace_tools(
            available_workspace_tools,
            mode_config["workspace_tools"],
            agent_id=managed.agent_id,
            mode=mode,
        )

        if task is not None:
            await self.task_service.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type=f"subagent_{mode}_started",
                stage="main_agent",
                message=f"{mode.title()} call started for {managed.agent_id}.",
                payload={
                    "instance_id": managed.instance_id,
                    "skill_package": mode_config["skill_package"],
                },
                agent_id=managed.agent_id,
                source={"type": "managed_agent", "id": managed.instance_id},
            )

        child_message = message
        if shared_context:
            child_message = (
                f"{message}\n\nContext explicitly shared by the MainAgent:\n{shared_context}"
            )
        child_service = _ScopedSingleAgentService(
            self.options,
            runtime_tools=runtime_tools,
            runtime_task_prompt=runtime_task_prompt,
            tenant_id=self.tenant_id,
        )
        child_request = SchedulingChatRequest(
            message=child_message,
            model=model,
            thread_id=managed.child_thread_id,
            session_id=managed.child_session_id,
            user_id=user_id,
            stream=event_sink is not None,
            skill_package=mode_config["skill_package"],
            extra_tools=mode_config["extra_tools"],
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

        _require_successful_managed_tool(
            managed.agent_id,
            mode,
            mode_config["required_success_tool"],
            successful_tools,
        )
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
                payload={
                    "instance_id": managed.instance_id,
                    "skill_package": mode_config["skill_package"],
                },
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
        mode: str,
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
        _delegation_mode_config(profile, mode)
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


def _validate_formal_script_task(
    task,
    *,
    user_id: str,
    tenant_id: str,
    workspace_id: str,
    instruction: str,
) -> None:
    if task is None:
        raise ValueError("Formal script task not found.")
    if task.user_id != user_id or task.tenant_id != tenant_id:
        raise ValueError(f"Task '{task.id}' is not available to this user.")
    if task.task_type != "media.script.generate" or task.handler_name != "external":
        raise ValueError(f"Task '{task.id}' is not a MainAgent script task.")
    if task.agent_id != MAIN_RUNTIME_AGENT_ID:
        raise ValueError(f"Task '{task.id}' is not assigned to MainAgent.")
    if task.task_key != "media_script":
        raise ValueError(f"Task '{task.id}' must use stable task_key 'media_script'.")
    task_input = task.input_payload_json or {}
    if str(task_input.get("workspace_id") or "") != workspace_id:
        raise ValueError(f"Task '{task.id}' does not belong to this Workspace.")
    expected_instruction = str(task_input.get("instruction") or "").strip()
    if expected_instruction != instruction.strip():
        raise ValueError(f"Task '{task.id}' instruction does not match this request.")


def _delegation_mode_config(profile: AgentProfileEntity, mode: str) -> dict[str, Any]:
    delegation = profile.runtime_config.get("delegation") or {}
    if not delegation.get("enabled"):
        raise ValueError(f"Agent '{profile.agent_id}' is not enabled for delegation.")
    modes = delegation.get("modes") or {}
    config = modes.get(mode) if isinstance(modes, dict) else None
    if not isinstance(config, dict):
        raise ValueError(f"Agent '{profile.agent_id}' does not support mode '{mode}'.")

    skill_package = str(config.get("skill_package") or "").strip()
    if not skill_package:
        raise ValueError(
            f"Agent '{profile.agent_id}' mode '{mode}' has no Skill Package configured."
        )
    configured_workspace_tools = config.get("workspace_tools")
    if not isinstance(configured_workspace_tools, list):
        raise ValueError(
            f"Agent '{profile.agent_id}' mode '{mode}' has no valid Workspace tool list configured."
        )
    workspace_tools = [
        str(name).strip()
        for name in configured_workspace_tools
        if str(name).strip()
    ]
    configured_extra_tools = config.get("extra_tools", [])
    if not isinstance(configured_extra_tools, list):
        raise ValueError(
            f"Agent '{profile.agent_id}' mode '{mode}' has no valid extra tool list configured."
        )
    extra_tools = [
        str(name).strip()
        for name in configured_extra_tools
        if str(name).strip()
    ]
    required_success_tool = str(config.get("required_success_tool") or "").strip()
    if required_success_tool and required_success_tool not in workspace_tools:
        raise ValueError(
            f"Agent '{profile.agent_id}' mode '{mode}' requires tool "
            f"'{required_success_tool}' but does not allow it."
        )
    return {
        "skill_package": skill_package,
        "extra_tools": extra_tools,
        "workspace_tools": workspace_tools,
        "required_success_tool": required_success_tool,
    }


def _select_workspace_tools(
    available_tools: list[FunctionTool],
    requested_names: list[str],
    *,
    agent_id: str,
    mode: str,
) -> list[FunctionTool]:
    available = {tool.metadata.name: tool for tool in available_tools}
    missing = [name for name in requested_names if name not in available]
    if missing:
        raise ValueError(
            f"Agent '{agent_id}' mode '{mode}' requires unavailable Workspace tools: "
            + ", ".join(missing)
        )
    return [available[name] for name in requested_names]


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


def _require_successful_managed_tool(
    agent_id: str,
    mode: str,
    required_tool: str,
    successful_tools: set[str],
) -> None:
    if not required_tool:
        return
    if required_tool not in successful_tools:
        raise ValueError(
            f"Agent '{agent_id}' mode '{mode}' completed without a successful "
            f"{required_tool} call."
        )
