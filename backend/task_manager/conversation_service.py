from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func
from sqlmodel import select

from db.db_context import create_db_session
from service.conversation import ConversationManager, ConversationThreadMessages

from .models import TaskEntity
from .registry import get_task_definition


VISIBLE_ROLES = {"user", "assistant"}


class TaskConversationService:
    """Read-only Task projection over durable SingleAgent conversations."""

    async def list_conversations(
        self,
        *,
        task_type: str,
        user_id: str,
        tenant_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        _validate_task_type(task_type)
        async with create_db_session() as session:
            latest_update = func.max(TaskEntity.updated_at).label("latest_update")
            grouped = await session.exec(
                select(TaskEntity.thread_id, latest_update)
                .where(TaskEntity.task_type == task_type)
                .where(TaskEntity.user_id == user_id)
                .where(TaskEntity.tenant_id == tenant_id)
                .where(TaskEntity.thread_id.is_not(None))
                .group_by(TaskEntity.thread_id)
                .order_by(desc(latest_update))
                .offset(offset)
                .limit(limit + 1)
            )
            grouped_rows = list(grouped.all())

            has_more = len(grouped_rows) > limit
            selected_rows = grouped_rows[:limit]
            thread_ids = [str(row[0]) for row in selected_rows if row[0]]
            if not thread_ids:
                return [], has_more

            task_result = await session.exec(
                select(TaskEntity)
                .where(TaskEntity.task_type == task_type)
                .where(TaskEntity.user_id == user_id)
                .where(TaskEntity.tenant_id == tenant_id)
                .where(TaskEntity.thread_id.in_(thread_ids))
                .order_by(desc(TaskEntity.updated_at), desc(TaskEntity.created_at))
            )
            latest_tasks: dict[str, TaskEntity] = {}
            for task in task_result.all():
                if task.thread_id:
                    latest_tasks.setdefault(task.thread_id, task)

        manager = ConversationManager(tenant_id=tenant_id)
        conversations: list[dict[str, Any]] = []
        for thread_id in thread_ids:
            conversation = await manager.get_thread_messages(thread_id)
            latest_task = latest_tasks.get(thread_id)
            if (
                conversation is None
                or conversation.thread.user_id != user_id
                or latest_task is None
            ):
                continue
            conversations.append(
                _conversation_summary(
                    task_type=task_type,
                    conversation=conversation,
                    latest_task=latest_task,
                )
            )
        return conversations, has_more

    async def get_conversation(
        self,
        *,
        thread_id: str,
        task_type: str,
        user_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        _validate_task_type(task_type)
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskEntity)
                .where(TaskEntity.thread_id == thread_id)
                .where(TaskEntity.task_type == task_type)
                .where(TaskEntity.user_id == user_id)
                .where(TaskEntity.tenant_id == tenant_id)
                .order_by(desc(TaskEntity.updated_at), desc(TaskEntity.created_at))
                .limit(1)
            )
            latest_task = result.first()
        if latest_task is None:
            return None

        conversation = await ConversationManager(
            tenant_id=tenant_id
        ).get_thread_messages(thread_id)
        if conversation is None or conversation.thread.user_id != user_id:
            return None

        visible_messages = [
            message for message in conversation.messages if message.role in VISIBLE_ROLES
        ]
        updated_at = _latest_datetime(
            conversation.thread.updated_at,
            latest_task.updated_at,
            *(message.created_at for message in visible_messages),
        )
        return {
            "conversation": {
                "thread_id": conversation.thread.id,
                "task_type": task_type,
                "title": conversation.thread.title or "New conversation",
                "created_at": _isoformat(conversation.thread.created_at),
                "updated_at": _isoformat(updated_at),
            },
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "text": message.text,
                    "attachments": message.attachments,
                    "created_at": _isoformat(message.created_at),
                }
                for message in visible_messages
            ],
            "latest_task": _latest_task_summary(latest_task),
        }


def _conversation_summary(
    *,
    task_type: str,
    conversation: ConversationThreadMessages,
    latest_task: TaskEntity,
) -> dict[str, Any]:
    visible_messages = [
        message for message in conversation.messages if message.role in VISIBLE_ROLES
    ]
    preview_message = next(
        (message for message in reversed(visible_messages) if message.role == "assistant"),
        visible_messages[-1] if visible_messages else None,
    )
    updated_at = _latest_datetime(
        conversation.thread.updated_at,
        latest_task.updated_at,
        *(message.created_at for message in visible_messages),
    )
    return {
        "thread_id": conversation.thread.id,
        "task_type": task_type,
        "title": conversation.thread.title or "New conversation",
        "preview": _preview(preview_message.text) if preview_message else "",
        "message_count": len(visible_messages),
        "latest_task": _latest_task_summary(latest_task),
        "created_at": _isoformat(conversation.thread.created_at),
        "updated_at": _isoformat(updated_at),
    }


def _latest_task_summary(task: TaskEntity) -> dict[str, Any]:
    run_id = task.current_run_id
    return {
        "task_id": task.id,
        "status": task.status,
        "run_id": run_id,
        "stream_url": (
            f"/task-manager/runs/{run_id}/events/stream"
            if run_id and task.status == "running"
            else None
        ),
    }


def _validate_task_type(task_type: str) -> None:
    get_task_definition(task_type)


def _preview(text: str, max_chars: int = 160) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars]}…"


def _latest_datetime(*values: datetime) -> datetime:
    return max(values, key=_datetime_sort_key)


def _datetime_sort_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        return f"{value.isoformat()}Z"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
