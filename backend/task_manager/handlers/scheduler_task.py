from __future__ import annotations

import json
from typing import Any, AsyncIterator

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService

from task_manager.handlers.base import TaskHandlerEvent
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

        request = SchedulingChatRequest(
            message=_build_task_message(task, definition),
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
                "Continue the conversation as a media script task assistant.",
                "If the user asks for edits, selection, or risk checks, use the existing thread/session context.",
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
                "Run an AI search chat task using the configured web search tool.",
                "Call the web search tool for the user's search request before producing final results.",
                "Rank and filter search results according to the user's goal.",
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


def _extract_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")
