from __future__ import annotations

from typing import Any

from sqlmodel import select

from db.db_context import create_db_session

from .models import TaskEntity, TaskItemEntity, utc_now


async def ensure_stage_items(
    task_id: str,
    *,
    item_type: str,
    raw_items: list[dict[str, Any]],
) -> None:
    """Create one idempotent, stage-local item set for a Pipeline batch stage."""

    async with create_db_session() as session:
        existing = await session.exec(
            select(TaskItemEntity)
            .where(TaskItemEntity.task_id == task_id)
            .where(TaskItemEntity.item_type == item_type)
        )
        if existing.first() is not None:
            return
        for index, payload in enumerate(raw_items, start=1):
            item_key = (
                payload.get("id")
                or payload.get("key")
                or payload.get("name")
                or payload.get("title")
                or f"{item_type}-{index}"
            )
            session.add(
                TaskItemEntity(
                    task_id=task_id,
                    item_type=item_type,
                    item_key=str(item_key),
                    sequence=index,
                    input_payload_json=payload,
                )
            )
        await session.commit()


async def list_pending_items(task_id: str, *, item_type: str | None = None) -> list[TaskItemEntity]:
    async with create_db_session() as session:
        statement = (
            select(TaskItemEntity)
            .where(TaskItemEntity.task_id == task_id)
            .where(TaskItemEntity.status == "pending")
        )
        if item_type is not None:
            statement = statement.where(TaskItemEntity.item_type == item_type)
        statement = statement.order_by(TaskItemEntity.sequence)
        result = await session.exec(statement)
        return list(result.all())


async def mark_item_running(item_id: str, run_id: str | None) -> TaskItemEntity:
    async with create_db_session() as session:
        item = await session.get(TaskItemEntity, item_id)
        if item is None:
            raise ValueError(f"Task item '{item_id}' not found.")
        now = utc_now()
        item.status = "running"
        item.run_id = run_id
        item.started_at = item.started_at or now
        item.updated_at = now
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def finish_item_success(item_id: str, result: dict[str, Any]) -> TaskItemEntity:
    async with create_db_session() as session:
        item = await session.get(TaskItemEntity, item_id)
        if item is None:
            raise ValueError(f"Task item '{item_id}' not found.")
        now = utc_now()
        item.status = "succeeded"
        item.result_payload_json = result
        item.error_payload_json = None
        item.finished_at = now
        item.updated_at = now
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def finish_item_failed(item_id: str, error: dict[str, Any]) -> TaskItemEntity:
    async with create_db_session() as session:
        item = await session.get(TaskItemEntity, item_id)
        if item is None:
            raise ValueError(f"Task item '{item_id}' not found.")
        now = utc_now()
        item.status = "failed"
        item.error_payload_json = error
        item.finished_at = now
        item.updated_at = now
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def refresh_task_progress(task_id: str) -> tuple[int, int]:
    async with create_db_session() as session:
        task = await session.get(TaskEntity, task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        result = await session.exec(
            select(TaskItemEntity).where(TaskItemEntity.task_id == task_id)
        )
        items = list(result.all())
        total = len(items) or 1
        completed = len([item for item in items if item.status in {"succeeded", "failed", "skipped"}])
        task.progress_total = total
        task.progress_current = completed
        task.updated_at = utc_now()
        session.add(task)
        await session.commit()
        return completed, total


async def load_item_results(task_id: str, *, item_type: str | None = None) -> list[dict[str, Any]]:
    async with create_db_session() as session:
        statement = select(TaskItemEntity).where(TaskItemEntity.task_id == task_id)
        if item_type is not None:
            statement = statement.where(TaskItemEntity.item_type == item_type)
        result = await session.exec(statement.order_by(TaskItemEntity.sequence))
        rows = list(result.all())
        return [
            {
                "item_id": item.id,
                "item_key": item.item_key,
                "item_type": item.item_type,
                "sequence": item.sequence,
                "status": item.status,
                "input": item.input_payload_json,
                "result": item.result_payload_json,
                "error": item.error_payload_json,
            }
            for item in rows
        ]
