from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from aituge_model.config import load_model_registry
from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import (
    RuntimeContextBlock,
    SchedulingChatRequest,
    SchedulingRuntimeOptions,
    SchedulingService,
)
from loguru import logger
from service.conversation.llm_runner import LlmRuntime
from service.structured_form import (
    AiFormCommandInterpreter,
    can_interpret_form_command,
)

from task_manager.handlers.base import TaskExecutionContext, TaskHandlerEvent
from task_manager.artifact_service import TaskArtifactPublisher
from task_manager.models import TaskEntity, utc_now
from task_manager.pipeline.store import create_stage_run, update_stage_run
from task_manager.registry import TaskType
from tool.artifacts import extract_artifacts


TOOL_ARGUMENT_MAX_CHARS = 4_000
LOCAL_PROOF_QA_INCOMPATIBLE_TOOLS = frozenset({"html_report_renderer"})


class SchedulerTaskHandler:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def stream(
        self,
        *,
        context: TaskExecutionContext,
    ) -> AsyncIterator[TaskHandlerEvent]:
        task = context.task
        definition = context.task_type
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, task.agent_id or definition.default_agent_id)

        if profile is None or not profile.enabled:
            raise ValueError(f"Agent profile '{task.agent_id or definition.default_agent_id}' is not available.")
        if profile.agent_type != "single":
            raise ValueError(f"Agent profile '{profile.agent_id}' has unsupported type '{profile.agent_type}'.")
        profile.default_tools_json = json.dumps(
            _compatible_scheduler_tools(task, profile.default_tools)
        )

        task_message, task_input_context = _build_scheduler_input(task, definition)
        runtime_context = context.runtime_context
        if task_input_context:
            runtime_context = runtime_context.extend(
                RuntimeContextBlock(
                    kind="task_input",
                    content=task_input_context,
                    metadata={"task_id": task.id, "task_type": task.task_type},
                )
            )

        request = SchedulingChatRequest(
            message=task_message,
            user_id=task.user_id,
            thread_id=task.thread_id,
            session_id=task.session_id,
            stream=True,
            model=_selected_model_id(task),
            skill_package=definition.default_skill_package,
            extra_tools=_compatible_scheduler_tools(task, definition.default_tools),
            extra_datasets=definition.default_datasets,
        )

        run_id = task.current_run_id
        if not run_id:
            raise ValueError("Scheduler Task has no active Run.")
        stage_run = await create_stage_run(
            task_id=task.id,
            run_id=run_id,
            stage_id="agent",
            stage_type="agent",
            attempt=max(task.attempt_count, 1),
            agent_id=profile.agent_id,
            input_artifact_ids=[],
        )
        artifact_publisher = TaskArtifactPublisher(
            root=self.options.local_python_artifact_dir,
            task_id=task.id,
            run_id=run_id,
            stage_run_id=stage_run.id,
        )
        started = time.perf_counter()

        task_payload = dict(task.input_payload_json or {})
        form_decision = None
        if can_interpret_form_command(task_payload):
            progress = "正在理解您的要求并核对当前表单，确认字段和取值后会立即执行并反馈结果。\n\n"
            yield TaskHandlerEvent(
                event_type="stream_chunk",
                stage="agent_stream",
                message="AI form command interpretation started.",
                step_id="form_command_interpretation",
                step_index=15,
                payload={"source_event": "ai_form_command_progress"},
                delta=progress,
                thread_id=task.thread_id,
                session_id=task.session_id,
                stage_run_id=stage_run.id,
                agent_id=profile.agent_id,
            )
            interpreter = AiFormCommandInterpreter(
                LlmRuntime(
                    tenant_id=task.tenant_id,
                    model_pack_id=task.model_pack_id,
                )
            )
            try:
                form_decision = await interpreter.interpret(
                    task_payload,
                    task_message,
                    model_id=request.model or profile.model_id,
                    trace_id=f"task-{task.id}-form-command",
                )
            except Exception as exc:
                logger.warning(
                    "AI form command interpretation failed; falling back to ReAct: "
                    "task_id={}, error={}",
                    task.id,
                    exc.__class__.__name__,
                )

        if form_decision is not None and form_decision.action in {
            "apply_changes",
            "clarify",
        }:
            for form_event in _form_command_events(
                decision=form_decision,
                task=task,
                task_payload=task_payload,
                stage_run_id=stage_run.id,
                agent_id=profile.agent_id,
            ):
                yield form_event
            await update_stage_run(
                stage_run.id,
                status="completed",
                finished_at=utc_now(),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return

        yield TaskHandlerEvent(
            event_type="scheduler_request_built",
            stage="scheduler_request_build",
            message="Scheduler request built.",
            step_id="scheduler_request_build",
            step_index=10,
            payload={
                "agent_id": profile.agent_id,
                "model_id": request.model or profile.model_id,
                "skill_package": request.skill_package,
                "primary_skill": definition.default_primary_skill,
                "candidate_skills": definition.default_candidate_skills,
                "extra_tools": request.extra_tools,
                "extra_datasets": request.extra_datasets,
                "task_memory_version": context.memory_view.version,
            },
            stage_run_id=stage_run.id,
            agent_id=profile.agent_id,
        )

        service = SchedulingService(
            self.options,
            tenant_id=task.tenant_id,
            model_pack_id=task.model_pack_id,
        )
        try:
            async for event in service.stream_chat(
                profile,
                request,
                runtime_context=runtime_context,
                artifact_publisher=artifact_publisher,
            ):
                if event.event == "metadata":
                    await update_stage_run(
                        stage_run.id,
                        thread_id=event.thread_id,
                        session_id=event.session_id,
                    )
                    yield TaskHandlerEvent(
                        event_type="agent_metadata",
                        stage="agent_stream",
                        message="Agent stream metadata received.",
                        step_id="agent_metadata",
                        step_index=20,
                        payload={"source_event": "metadata", "agent_id": profile.agent_id},
                        thread_id=event.thread_id,
                        session_id=event.session_id,
                        stage_run_id=stage_run.id,
                        agent_id=profile.agent_id,
                    )
                    continue

                if event.event == "final":
                    data = event.data or {}
                    content = str(data.get("content") or "")
                    usage = data.get("usage")
                    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), list) else []
                    yield TaskHandlerEvent(
                        event_type="agent_final",
                        stage="agent_stream",
                        message="Agent stream finished.",
                        step_id="agent_final",
                        step_index=40,
                        payload={
                            "source_event": "final",
                            "content_chars": len(content),
                            "usage": usage or {},
                            "artifacts": artifacts[:20],
                        },
                        thread_id=event.thread_id,
                        session_id=event.session_id,
                        final_content=content,
                        usage=usage,
                        token_usage=usage,
                        stage_run_id=stage_run.id,
                        agent_id=profile.agent_id,
                    )
                    continue

                for translated in _translate_chunk_event(event):
                    translated.stage_run_id = stage_run.id
                    translated.agent_id = profile.agent_id
                    yield translated
        except Exception as exc:
            await update_stage_run(
                stage_run.id,
                status="failed",
                error_code=str(getattr(exc, "code", exc.__class__.__name__)),
                error_message=str(exc),
                finished_at=utc_now(),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        await update_stage_run(
            stage_run.id,
            status="completed",
            finished_at=utc_now(),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def _build_task_message(task: TaskEntity, definition: TaskType) -> str:
    payload = dict(task.input_payload_json or {})
    task_type = task.task_type
    pretty_payload = json.dumps(payload, ensure_ascii=False, indent=2)

    if task_type == "ai.search.chat":
        user_message = payload.get("message") or ""
        search_goal = payload.get("search_goal") or ""
        max_results = payload.get("max_results") or 5
        return "\n".join(
            [
                "Run a source-backed AI search task using the configured web search tool.",
                "Use the ai-search skill as a freshness-first source discovery workflow.",
                "First plan 1-3 executable Chinese search queries with reasons. Then call the web search tool.",
                "Use at most 3 web search tool calls for one task, then select the best source cards.",
                "Preserve the user's concrete search intent and main nouns/entities in every planned query.",
                "If the user message is unreadable or too ambiguous, return status='needs_clarification' instead of searching a guessed broad topic.",
                "Inspect source authority, freshness, and relevance before producing final results.",
                "Put weak evidence and caveats in evidence_summary or risks.",
                "Return exactly one valid JSON object matching the ai_search_output schema.",
                "Do not add Markdown or explanation outside the JSON.",
                "The JSON must parse with json.loads. Do not put raw ASCII double quotes inside string values; escape them or use Chinese quotes.",
                "",
                f"Task title: {task.title or definition.name}",
                f"User search message: {user_message}",
                f"Search goal: {search_goal}",
                f"Maximum result cards: {max_results}",
                "Full task input:",
                pretty_payload,
            ]
        )

    return "\n".join(
        [
            f"Execute task_type: {task_type}",
            f"Task title: {task.title or definition.name}",
            "Task input:",
            pretty_payload,
        ]
    )


def _build_scheduler_input(task: TaskEntity, definition: TaskType) -> tuple[str, str]:
    """Separate the durable user message from internal Task execution parameters."""

    field_name = definition.conversation_message_field
    if not field_name:
        return _build_task_message(task, definition), ""

    payload = dict(task.input_payload_json or {})
    raw_message = payload.get(field_name)
    if not isinstance(raw_message, str) or not raw_message.strip():
        raise ValueError(
            f"Task type '{task.task_type}' requires a non-empty string input field "
            f"'{field_name}'."
        )

    execution_parameters = {
        key: value
        for key, value in payload.items()
        if key not in {field_name, "model_id"}
    }
    context_lines = [
        "Internal Task execution context. Use it to execute the request, but do not quote "
        "or treat it as part of the user's message.",
        f"Task type: {task.task_type}",
    ]
    if execution_parameters:
        context_lines.extend(
            [
                "Execution parameters:",
                json.dumps(execution_parameters, ensure_ascii=False, indent=2),
            ]
        )
    return raw_message.strip(), "\n".join(context_lines)


def _selected_model_id(task: TaskEntity) -> str | None:
    value = (task.input_payload_json or {}).get("model_id")
    normalized = str(value or "").strip()
    return normalized or None


def _compatible_scheduler_tools(task: TaskEntity, declared_tools: list[str]) -> list[str]:
    """Remove tools whose schemas cannot be compiled by the selected local LLM."""

    tools = list(declared_tools)
    if task.task_type != "proof.qa.chat":
        return tools

    pack = load_model_registry().resolve_pack(task.model_pack_id)
    if pack.llm.mode != "local":
        return tools

    return [tool for tool in tools if tool not in LOCAL_PROOF_QA_INCOMPATIBLE_TOOLS]


def _extract_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")


def _translate_chunk_event(event) -> list[TaskHandlerEvent]:
    data = event.data if isinstance(event.data, dict) else {}
    delta = _extract_delta(data)
    if delta:
        return [
            TaskHandlerEvent(
                event_type="stream_chunk",
                stage="agent_stream",
                message="Agent stream chunk received.",
                step_id="agent_stream",
                step_index=30,
                payload={"source_event": "chunk"},
                delta=delta,
                thread_id=event.thread_id,
                session_id=event.session_id,
            )
        ]

    translated: list[TaskHandlerEvent] = []
    for action in data.get("actions") or []:
        if not isinstance(action, dict):
            continue
        tool_name, tool_call_id = _tool_identity(action)
        translated.append(
            TaskHandlerEvent(
                event_type="tool_started",
                stage="tool_execution",
                message=f"Tool '{tool_name}' started.",
                step_id=f"tool_started:{tool_call_id or tool_name}",
                step_index=25,
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "arguments": _bounded_tool_arguments(action),
                    "status": "started",
                },
                thread_id=event.thread_id,
                session_id=event.session_id,
            )
        )

    observation = data.get("observation")
    if isinstance(observation, dict):
        tool = observation.get("tool") if isinstance(observation.get("tool"), dict) else {}
        tool_name, tool_call_id = _tool_identity(tool)
        result = observation.get("result")
        error = str(observation.get("error") or "")
        translated.append(
            TaskHandlerEvent(
                event_type="tool_completed",
                stage="tool_execution",
                message=f"Tool '{tool_name}' completed." if not error else f"Tool '{tool_name}' returned an error.",
                level="warning" if error else "info",
                step_id=f"tool_completed:{tool_call_id or tool_name}",
                step_index=35,
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "arguments": _bounded_tool_arguments(tool),
                    "status": "failed" if error else "completed",
                    "result_chars": len(result) if isinstance(result, str) else 0,
                    "artifacts": extract_artifacts(result),
                    "error": error[:500],
                },
                thread_id=event.thread_id,
                session_id=event.session_id,
            )
        )

    # Reasoning tokens, cumulative citations and raw observations are intentionally omitted.
    return translated


def _tool_identity(payload: dict[str, Any]) -> tuple[str, str]:
    function = payload.get("function") if isinstance(payload.get("function"), dict) else {}
    return str(function.get("name") or "unknown_tool"), str(payload.get("id") or "")


def _bounded_tool_arguments(payload: dict[str, Any]) -> str:
    function = payload.get("function") if isinstance(payload.get("function"), dict) else {}
    arguments = function.get("arguments")
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        text = arguments
    else:
        text = json.dumps(arguments, ensure_ascii=False)
    if len(text) <= TOOL_ARGUMENT_MAX_CHARS:
        return text
    return (
        text[:TOOL_ARGUMENT_MAX_CHARS]
        + f"...（参数已截断，原始 {len(text)} 字符）"
    )


def _form_command_events(
    *,
    decision: Any,
    task: TaskEntity,
    task_payload: dict[str, Any],
    stage_run_id: str,
    agent_id: str,
) -> list[TaskHandlerEvent]:
    events: list[TaskHandlerEvent] = []
    if decision.action == "apply_changes":
        tool_call_id = f"ai-form-change:{task.id}"
        arguments = json.dumps(
            decision.arguments(
                draft_id=str(task_payload["active_resource_id"]),
                expected_version=int(task_payload["draft_version"]),
            ),
            ensure_ascii=False,
        )
        common_payload = {
            "tool_name": "apply_form_changes",
            "tool_call_id": tool_call_id,
            "arguments": arguments,
        }
        events.extend(
            [
                TaskHandlerEvent(
                    event_type="tool_started",
                    stage="tool_execution",
                    message="Tool 'apply_form_changes' started.",
                    step_id=f"tool_started:{tool_call_id}",
                    step_index=25,
                    payload={**common_payload, "status": "started"},
                    thread_id=task.thread_id,
                    session_id=task.session_id,
                    stage_run_id=stage_run_id,
                    agent_id=agent_id,
                ),
                TaskHandlerEvent(
                    event_type="tool_completed",
                    stage="tool_execution",
                    message="Tool 'apply_form_changes' completed.",
                    step_id=f"tool_completed:{tool_call_id}",
                    step_index=35,
                    payload={
                        **common_payload,
                        "status": "completed",
                        "result_chars": 0,
                        "artifacts": [],
                        "error": "",
                    },
                    thread_id=task.thread_id,
                    session_id=task.session_id,
                    stage_run_id=stage_run_id,
                    agent_id=agent_id,
                ),
            ]
        )
        content = decision.success_message()
        source_event = "ai_form_change"
        final_message = "AI form edit completed."
    else:
        content = decision.clarification
        source_event = "ai_form_clarification"
        final_message = "AI form clarification completed."

    metrics = {
        "source_event": source_event,
        "content_chars": len(content),
        "model_duration_ms": decision.model_duration_ms,
        "time_to_first_token_ms": decision.time_to_first_token_ms,
    }
    events.extend(
        [
            TaskHandlerEvent(
                event_type="stream_chunk",
                stage="agent_stream",
                message="AI form response emitted.",
                step_id="agent_stream",
                step_index=30,
                payload=metrics,
                delta=content,
                thread_id=task.thread_id,
                session_id=task.session_id,
                stage_run_id=stage_run_id,
                agent_id=agent_id,
            ),
            TaskHandlerEvent(
                event_type="agent_final",
                stage="agent_stream",
                message=final_message,
                step_id="agent_final",
                step_index=40,
                payload=metrics,
                thread_id=task.thread_id,
                session_id=task.session_id,
                final_content=content,
                usage=decision.usage,
                token_usage=decision.usage,
                stage_run_id=stage_run_id,
                agent_id=agent_id,
            ),
        ]
    )
    return events
