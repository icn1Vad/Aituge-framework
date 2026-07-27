from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text
from sqlmodel import select

from task_manager.models import TaskEntity, TaskQuotaEntity, TaskRunEntity, utc_now


DEFAULT_TENANT_CONCURRENCY = 2


async def ensure_quota_scope(
    session: Any,
    *,
    service: str,
    tenant_id: str,
    resource_pool: str,
    max_concurrency: int,
) -> None:
    """Create a scope row once; the unique key makes concurrent creation safe."""

    await session.execute(
        text(
            "INSERT INTO tuge_task_quota "
            "(id, service, tenant_id, resource_pool, max_concurrency, running_count, "
            "created_at, updated_at) "
            "VALUES (:id, :service, :tenant_id, :resource_pool, :max_concurrency, 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (service, tenant_id, resource_pool) DO NOTHING"
        ),
        {
            "id": uuid.uuid4().hex,
            "service": service,
            "tenant_id": tenant_id,
            "resource_pool": resource_pool,
            "max_concurrency": max(1, max_concurrency),
        },
    )


async def lock_quota_scope(
    session: Any,
    *,
    service: str,
    tenant_id: str,
    resource_pool: str,
    max_concurrency: int,
) -> TaskQuotaEntity:
    await ensure_quota_scope(
        session,
        service=service,
        tenant_id=tenant_id,
        resource_pool=resource_pool,
        max_concurrency=max_concurrency,
    )
    result = await session.exec(
        select(TaskQuotaEntity)
        .where(TaskQuotaEntity.service == service)
        .where(TaskQuotaEntity.tenant_id == tenant_id)
        .where(TaskQuotaEntity.resource_pool == resource_pool)
        .with_for_update()
    )
    quota = result.first()
    if quota is None:
        raise RuntimeError("Task quota scope disappeared after creation.")
    return quota


async def claim_fair_run(
    session: Any,
    *,
    worker_id: str,
    lease_seconds: int,
    tenant_concurrency: int,
) -> tuple[str, str, int] | None:
    candidate_statement = (
        select(
            TaskEntity.service,
            TaskEntity.tenant_id,
            TaskRunEntity.resource_pool,
            func.min(TaskEntity.created_at),
        )
        .join(TaskEntity, TaskEntity.id == TaskRunEntity.task_id)
        .where(TaskRunEntity.status == "running")
        .where(TaskRunEntity.cancel_requested.is_(False))
        .where(TaskEntity.status == "running")
        .where(TaskEntity.handler_name != "external")
        .where(TaskEntity.cancel_requested.is_(False))
        .where(TaskEntity.current_run_id == TaskRunEntity.id)
        .group_by(TaskEntity.service, TaskEntity.tenant_id, TaskRunEntity.resource_pool)
    )
    candidate_result = await session.exec(candidate_statement)
    candidates = [
        (row[0], row[1], row[2] or "default", row[3])
        for row in candidate_result.all()
    ]
    if not candidates:
        return None

    quota_result = await session.exec(select(TaskQuotaEntity))
    quota_by_key = {
        (row.service, row.tenant_id, row.resource_pool): row
        for row in quota_result.all()
    }

    def fair_key(item):
        quota = quota_by_key.get((item[0], item[1], item[2]))
        return (
            quota.last_scheduled_at if quota and quota.last_scheduled_at else datetime.min,
            item[3],
            item[0],
            item[1],
            item[2],
        )

    candidates.sort(key=fair_key)
    for service, tenant_id, resource_pool, _oldest_created in candidates:
        quota = await lock_quota_scope(
            session,
            service=service,
            tenant_id=tenant_id,
            resource_pool=resource_pool,
            max_concurrency=tenant_concurrency,
        )
        statement = (
            select(TaskRunEntity)
            .join(TaskEntity, TaskEntity.id == TaskRunEntity.task_id)
            .where(TaskRunEntity.status == "running")
            .where(TaskRunEntity.cancel_requested.is_(False))
            .where(TaskEntity.status == "running")
            .where(TaskEntity.handler_name != "external")
            .where(TaskEntity.cancel_requested.is_(False))
            .where(TaskEntity.current_run_id == TaskRunEntity.id)
            .where(TaskEntity.service == service)
            .where(TaskEntity.tenant_id == tenant_id)
            .where(TaskRunEntity.resource_pool == resource_pool)
            .where(
                (TaskRunEntity.lease_until.is_(None))
                | (TaskRunEntity.lease_until <= utc_now())
            )
            .order_by(TaskEntity.priority.desc(), TaskEntity.created_at, TaskEntity.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await session.exec(statement)
        run = result.first()
        if run is None:
            continue
        holds_slot = not run.quota_slot_released
        if not holds_slot and quota.running_count >= quota.max_concurrency:
            continue
        task = await session.get(TaskEntity, run.task_id)
        if task is None:
            continue
        now = utc_now()
        run.lease_owner = worker_id
        run.lease_until = now + timedelta(seconds=lease_seconds)
        run.lease_version += 1
        run.last_heartbeat_at = now
        if not holds_slot:
            quota.running_count += 1
            run.quota_slot_released = False
        quota.last_scheduled_at = now
        quota.updated_at = now
        session.add(run)
        session.add(quota)
        await session.commit()
        return task.id, run.id, run.lease_version
    return None


async def release_quota_slot(
    session: Any,
    *,
    service: str,
    tenant_id: str,
    resource_pool: str,
    max_concurrency: int = DEFAULT_TENANT_CONCURRENCY,
) -> None:
    quota = await lock_quota_scope(
        session,
        service=service,
        tenant_id=tenant_id,
        resource_pool=resource_pool,
        max_concurrency=max_concurrency,
    )
    quota.running_count = max(0, quota.running_count - 1)
    quota.updated_at = utc_now()
    session.add(quota)
