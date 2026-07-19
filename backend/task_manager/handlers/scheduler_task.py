from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import (
    RuntimeContextBlock,
    SchedulingChatRequest,
    SchedulingRuntimeOptions,
    SchedulingService,
)

from task_manager.handlers.base import TaskExecutionContext, TaskHandlerEvent
from task_manager.artifact_service import TaskArtifactPublisher
from task_manager.models import TaskEntity, utc_now
from task_manager.pipeline.store import create_stage_run, update_stage_run
from task_manager.registry import TaskType
from tool.artifacts import extract_artifacts


TOOL_ARGUMENT_MAX_CHARS = 4_000


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
            skill_package=definition.default_skill_package,
            extra_tools=definition.default_tools,
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

        yield TaskHandlerEvent(
            event_type="scheduler_request_built",
            stage="scheduler_request_build",
            message="Scheduler request built.",
            step_id="scheduler_request_build",
            step_index=10,
            payload={
                "agent_id": profile.agent_id,
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

        service = SchedulingService(self.options)
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
                error_code=exc.__class__.__name__,
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

    if task_type == "media.script.generate":
        return "\n".join(
            [
                "Generate a short-video script from the media task input below.",
                "Follow the media-script-generator skill output structure and boundaries.",
                "Return exactly one valid JSON object. Do not add Markdown or explanation outside the JSON.",
                "",
                f"Task title: {task.title or definition.name}",
                "Task input:",
                pretty_payload,
            ]
        )

    if task_type == "media.script.select":
        return "\n".join(
            [
                "Select the best media script candidate from the input below.",
                "Follow the media-script-selector skill and return a structured selection result.",
                "Return exactly one valid JSON object. Do not add Markdown or explanation outside the JSON.",
                "",
                f"Task title: {task.title or definition.name}",
                "Task input:",
                pretty_payload,
            ]
        )

    if task_type == "media.chat":
        user_message = payload.get("message") or payload.get("question") or ""
        return "\n".join(
            [
                "Answer as a read-only media script conversation assistant.",
                "Use the media-script-chat skill and the bounded current-script context below.",
                "Do not rewrite or mutate the script, create artifacts, rerun a Pipeline, or claim that an edit was saved.",
                "If the user requests an edit, explain the suggested change in natural language only.",
                "Return a concise natural-language answer, not a complete script JSON object.",
                "",
                f"User message: {user_message}",
                "Additional input:",
                pretty_payload,
            ]
        )

    if task_type == "ai.search.chat":
        user_message = payload.get("message") or ""
        search_goal = payload.get("search_goal") or ""
        max_results = payload.get("max_results") or 5
        return "\n".join(
            [
                "Run a source-backed AI search task using the configured web search tool.",
                "Use the ai-search skill as a freshness-first search and new-media topic discovery workflow.",
                "First plan 1-3 executable Chinese search queries with reasons. Then call the web search tool.",
                "Use at most 3 web search tool calls for one task, then select the best source cards and topic suggestions.",
                "Preserve the user's concrete search intent and main nouns/entities in every planned query.",
                "Do not replace a specific search request with generic business-axis fallback topics.",
                "If the user message is unreadable or too ambiguous, return status='needs_clarification' instead of searching a guessed broad topic.",
                "Inspect source authority, freshness, relevance, and business bridge before producing final results.",
                "If the user is looking for new-media topics or reliable material sources, aggregate sources into topic_suggestions.",
                "Do not force weak sources into business conversion. Put weak evidence and caveats in evidence_summary or risks.",
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

    if task_type == "media.topic.search":
        user_message = payload.get("message") or payload.get("topic_query") or ""
        search_goal = payload.get("search_goal") or ""
        search_mode = payload.get("search_mode") or "specific_search"
        max_results = payload.get("max_results") or 5
        max_topics = payload.get("max_topics") or 5
        mode_instruction = (
            "Treat this as broad current-hotspot discovery. Search across recent signals and keep only naturally related topics."
            if search_mode == "hotspot_discovery"
            else "Treat this as a specific search. Preserve the user's concrete target in every query."
        )
        return "\n".join(
            [
                "Run a reusable new-media topic search task using the configured web search tool.",
                "Use the media-topic-search skill as the task authority.",
                "This task should produce reliable source cards and operator-ready topic suggestions.",
                "First plan 2-5 executable Chinese search queries with reasons. Then call the web search tool.",
                "Preserve the user's concrete search intent and main nouns/entities in every planned query.",
                "Do not replace a specific search request with generic business-axis fallback topics.",
                "If the user message is unreadable or too ambiguous, return status='needs_clarification' instead of searching a guessed broad topic.",
                "Inspect source authority, freshness, relevance, and media business bridge before producing final results.",
                f"Requested search mode: {search_mode}. This value is authoritative; query_plan.mode must equal it exactly.",
                mode_instruction,
                "Return exactly one valid JSON object matching the media_topic_search_output schema.",
                "Do not add Markdown or explanation outside the JSON.",
                "The JSON must parse with json.loads. Do not put raw ASCII double quotes inside string values; escape them or use Chinese quotes.",
                "",
                f"Task title: {task.title or definition.name}",
                f"User topic search message: {user_message}",
                f"Search goal: {search_goal}",
                f"Search mode: {search_mode}",
                f"Maximum source cards: {max_results}",
                f"Maximum topic suggestions: {max_topics}",
                "Full task input:",
                pretty_payload,
            ]
        )

    if task_type == "analytics.douyin.account_report.generate":
        analysis_scope = payload.get("analysis_scope") or "all_data"
        report_goal = payload.get("report_goal") or ""
        return "\n".join(
            [
                "Generate a fact-grounded Douyin account operations analysis report.",
                "Use the douyin-account-report skill as the task authority.",
                "Default to analyzing all available account data, not only one calendar month.",
                "Only use month or date range boundaries when analysis_scope explicitly requests them.",
                "Do not call any tools for this first TaskManager version. Do not call ReadSkill or code tools.",
                "Use only the data already present in Full task input.",
                "Do not invent metrics, audience profiles, comments, benchmarks, retention, or script quality evidence.",
                "If data is missing, explain the limitation and still produce a useful partial report.",
                "Return exactly one valid JSON object matching the douyin_account_report_output schema.",
                "Do not add Markdown or explanation outside the JSON.",
                "The JSON must parse with json.loads. Escape raw double quotes inside string values.",
                "Set export_markdown to an empty string for now; do not put multi-line Markdown inside JSON.",
                "Keep each section concise: summary plus up to 4 findings, 4 evidence strings, 4 limitations, and 4 next_actions.",
                "Keep the complete JSON under 6000 Chinese characters.",
                "top_content_analysis must contain at most 3 items. low_content_analysis must contain at most 2 items.",
                "Each top/low content item should only include id, title, play_count, reason, and recommended_action.",
                "",
                f"Task title: {task.title or definition.name}",
                f"Analysis scope: {analysis_scope}",
                f"Report goal: {report_goal}",
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
        key: value for key, value in payload.items() if key != field_name
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
