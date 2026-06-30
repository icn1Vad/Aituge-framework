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
            primary_skill=definition.default_primary_skill,
            candidate_skills=definition.default_candidate_skills,
            extra_tools=definition.default_tools,
            extra_datasets=definition.default_datasets,
        )

        yield TaskHandlerEvent(
            event_type="scheduler_request_built",
            stage="scheduler_request_build",
            message="Scheduler request built.",
            payload={
                "agent_id": profile.agent_id,
                "primary_skill": request.primary_skill,
                "candidate_skills": request.candidate_skills or [],
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
                    payload=payload,
                    thread_id=event.thread_id,
                    session_id=event.session_id,
                    final_content=str(data.get("content") or ""),
                    usage=data.get("usage"),
                )
                continue

            delta = _extract_delta(event.data or {})
            event_type = "stream_chunk" if delta else "agent_event"
            message = "Agent stream chunk received." if delta else f"Agent event '{event.event}' received."
            yield TaskHandlerEvent(
                event_type=event_type,
                stage="agent_stream",
                message=message,
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
                "请根据下面的新媒体任务输入生成短视频脚本。",
                "必须遵循 media-script-generator skill 的输出结构和边界要求。",
                "最终只输出一个 JSON 对象，不要在 JSON 外写 Markdown 或解释。",
                "",
                f"任务名称：{task.title or definition.name}",
                "任务输入：",
                pretty_payload,
            ]
        )

    if task_type == "media.script.select":
        return "\n".join(
            [
                "请根据下面的新媒体脚本候选做脚本选择。",
                "必须遵循 media-script-selector skill，返回结构化选择结果。",
                "最终只输出一个 JSON 对象，不要在 JSON 外写 Markdown 或解释。",
                "",
                f"任务名称：{task.title or definition.name}",
                "任务输入：",
                pretty_payload,
            ]
        )

    if task_type == "media.chat":
        user_message = payload.get("message") or payload.get("question") or ""
        return "\n".join(
            [
                "请作为新媒体脚本任务助手继续对话。",
                "如涉及脚本修改、选择或风险判断，请沿用已有 thread/session 上下文。",
                "",
                f"用户问题：{user_message}",
                "补充输入：",
                pretty_payload,
            ]
        )

    return "\n".join(
        [
            f"请执行任务类型：{task_type}",
            f"任务名称：{task.title or definition.name}",
            "任务输入：",
            pretty_payload,
        ]
    )


def _extract_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")
