from __future__ import annotations

import json

from sqlalchemy import desc
from sqlmodel import select

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService

from .models import TaskMemoryEntity
from .output_parser import parse_json_output


MEMORY_AGENT_ID = "media-writer-agent"
MEMORY_SKILL_PACKAGE = "media-script-memory-compression-package"


class TaskMemoryService:
    """Load and roll forward the shared memory for one stable business Task key."""

    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def get_latest(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_key: str,
    ) -> TaskMemoryEntity | None:
        normalized_key = _normalize_task_key(task_key)
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskMemoryEntity)
                .where(TaskMemoryEntity.tenant_id == tenant_id)
                .where(TaskMemoryEntity.user_id == user_id)
                .where(TaskMemoryEntity.task_key == normalized_key)
                .order_by(desc(TaskMemoryEntity.version))
                .limit(1)
            )
            return result.first()

    async def compress(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_key: str,
        new_information: str,
    ) -> TaskMemoryEntity:
        normalized_key = _normalize_task_key(task_key)
        information = (new_information or "").strip()
        if not information:
            raise ValueError("new_information must not be empty.")

        previous = await self.get_latest(
            tenant_id=tenant_id,
            user_id=user_id,
            task_key=normalized_key,
        )
        next_version = (previous.version if previous else 0) + 1
        message = _compression_message(
            task_key=normalized_key,
            previous_memory=previous.content if previous else "",
            new_information=information,
        )

        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, MEMORY_AGENT_ID)
        if profile is None or not profile.enabled:
            raise ValueError(f"Agent profile '{MEMORY_AGENT_ID}' is not available.")

        response = await SchedulingService(self.options, tenant_id=tenant_id).chat(
            profile,
            SchedulingChatRequest(
                message=message,
                user_id=user_id,
                session_id=f"task-memory:{normalized_key}:v{next_version}",
                stream=False,
                skill_package=MEMORY_SKILL_PACKAGE,
                extra_tools=[],
                extra_datasets=[],
            ),
        )
        content = _assistant_content(response)
        parsed = parse_json_output(content)
        if not parsed.ok or not isinstance(parsed.structured, dict):
            raise ValueError("Memory compression did not return a valid JSON object.")
        memory_text = str(parsed.structured.get("memory") or "").strip()
        if not memory_text:
            raise ValueError("Memory compression returned an empty memory.")

        row = TaskMemoryEntity(
            tenant_id=tenant_id,
            user_id=user_id,
            task_key=normalized_key,
            version=next_version,
            content=memory_text,
            source_text=information,
        )
        async with create_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row


def render_task_memory(memory: TaskMemoryEntity | None) -> str:
    if memory is None or not memory.content.strip():
        return ""
    return "\n".join(
        [
            "# Task Memory",
            "Use this shared memory as persistent background for the current business Task.",
            "The user's explicit instruction in the current request always takes priority.",
            memory.content.strip(),
        ]
    )


def _normalize_task_key(task_key: str) -> str:
    value = (task_key or "").strip()
    if not value:
        raise ValueError("task_key must not be empty.")
    if len(value) > 160:
        raise ValueError("task_key must be at most 160 characters.")
    return value


def _compression_message(*, task_key: str, previous_memory: str, new_information: str) -> str:
    return "\n".join(
        [
            "Compress the Task Memory using the active memory-compression skill.",
            "Return exactly one JSON object: {\"memory\": \"the complete updated memory\"}.",
            "Do not return Markdown or any text outside the JSON object.",
            f"Stable business Task ID: {task_key}",
            "Previous memory:",
            previous_memory.strip() or "(empty)",
            "New user-confirmed information:",
            new_information,
        ]
    )


def _assistant_content(response: dict) -> str:
    choices = ((response.get("response") or {}).get("choices") or [])
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    value = message.get("content")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False) if value is not None else ""
