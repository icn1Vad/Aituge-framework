from __future__ import annotations

import json
from typing import Any, AsyncIterator

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService

from task_manager.handlers.base import TaskHandlerEvent
from task_manager.memory import TaskMemoryService, render_task_memory
from task_manager.models import TaskEntity
from task_manager.registry import TaskDefinition


class SchedulerTaskHandler:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def stream(
        self,
        *,
        task: TaskEntity,
        definition: TaskDefinition,
    ) -> AsyncIterator[TaskHandlerEvent]:
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, task.agent_id or definition.default_agent_id)

        if profile is None or not profile.enabled:
            raise ValueError(f"Agent profile '{task.agent_id or definition.default_agent_id}' is not available.")
        if profile.agent_type != "single":
            raise ValueError(f"Agent profile '{profile.agent_id}' has unsupported type '{profile.agent_type}'.")

        memory = None
        if task.task_key:
            memory = await TaskMemoryService(self.options).get_latest(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                task_key=task.task_key,
            )
        task_message = _build_task_message(task, definition)
        memory_prompt = render_task_memory(memory)
        if memory_prompt:
            task_message = f"{memory_prompt}\n\n{task_message}"

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
                "task_memory_version": memory.version if memory else None,
            },
        )

        service = SchedulingService(self.options)
        async for event in service.stream_chat(profile, request):
            payload = event.model_dump(exclude_none=True)
            if event.event == "metadata":
                yield TaskHandlerEvent(
                    event_type="agent_metadata",
                    stage="agent_stream",
                    message="Agent stream metadata received.",
                    step_id="agent_metadata",
                    step_index=20,
                    payload=payload,
                    thread_id=event.thread_id,
                    session_id=event.session_id,
                )
                continue

            if event.event == "final":
                data = event.data or {}
                yield TaskHandlerEvent(
                    event_type="agent_final",
                    stage="agent_stream",
                    message="Agent stream finished.",
                    step_id="agent_final",
                    step_index=40,
                    payload=payload,
                    thread_id=event.thread_id,
                    session_id=event.session_id,
                    final_content=str(data.get("content") or ""),
                    usage=data.get("usage"),
                    token_usage=data.get("usage"),
                )
                continue

            delta = _extract_delta(event.data or {})
            event_type = "stream_chunk" if delta else "agent_event"
            message = "Agent stream chunk received." if delta else f"Agent event '{event.event}' received."
            yield TaskHandlerEvent(
                event_type=event_type,
                stage="agent_stream",
                message=message,
                step_id="agent_stream",
                step_index=30,
                payload=payload,
                delta=delta,
                thread_id=event.thread_id,
                session_id=event.session_id,
            )


def _build_task_message(task: TaskEntity, definition: TaskDefinition) -> str:
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
        max_results = payload.get("max_results") or 5
        max_topics = payload.get("max_topics") or 5
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
                "Return exactly one valid JSON object matching the media_topic_search_output schema.",
                "Do not add Markdown or explanation outside the JSON.",
                "The JSON must parse with json.loads. Do not put raw ASCII double quotes inside string values; escape them or use Chinese quotes.",
                "",
                f"Task title: {task.title or definition.name}",
                f"User topic search message: {user_message}",
                f"Search goal: {search_goal}",
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


def _extract_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")
