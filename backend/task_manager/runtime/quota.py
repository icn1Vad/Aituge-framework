from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, text
from sqlmodel import select

from task_manager.models import (
    TaskEntity,
    TaskQuotaEntity,
    TaskRunEntity,
    TaskUserScheduleEntity,
    utc_now,
)


DEFAULT_TENANT_CONCURRENCY = 10


async def ensure_quota_scope(
    session: Any,
    *,
    service: str,
    tenant_id: str,
    resource_pool: str,
    max_concurrency: int,
    sync_limit: bool = True,
) -> None:
    """Create a scope row once; the unique key makes concurrent creation safe."""

    conflict_clause = (
        "DO UPDATE SET max_concurrency = excluded.max_concurrency, updated_at = CURRENT_TIMESTAMP"
        if sync_limit
        else "DO NOTHING"
    )
    await session.execute(
        text(
            "INSERT INTO tuge_task_quota "
            "(id, service, tenant_id, resource_pool, max_concurrency, running_count, "
            "created_at, updated_at) "
            "VALUES (:id, :service, :tenant_id, :resource_pool, :max_concurrency, 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            f"ON CONFLICT (service, tenant_id, resource_pool) {conflict_clause}"
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
    sync_limit: bool = True,
) -> TaskQuotaEntity:
    await ensure_quota_scope(
        session,
        service=service,
        tenant_id=tenant_id,
        resource_pool=resource_pool,
        max_concurrency=max_concurrency,
        sync_limit=sync_limit,
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


async def lock_user_schedule_scope(
    session: Any,
    *,
    service: str,
    tenant_id: str,
    resource_pool: str,
    user_id: str,
) -> TaskUserScheduleEntity:
    await session.execute(
        text(
            "INSERT INTO tuge_task_user_schedule "
            "(id, service, tenant_id, resource_pool, user_id, last_scheduled_at, "
            "created_at, updated_at) "
            "VALUES (:id, :service, :tenant_id, :resource_pool, :user_id, NULL, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (service, tenant_id, resource_pool, user_id) DO NOTHING"
        ),
        {
            "id": uuid.uuid4().hex,
            "service": service,
            "tenant_id": tenant_id,
            "resource_pool": resource_pool,
            "user_id": user_id,
        },
    )
    result = await session.exec(
        select(TaskUserScheduleEntity)
        .where(TaskUserScheduleEntity.service == service)
        .where(TaskUserScheduleEntity.tenant_id == tenant_id)
        .where(TaskUserScheduleEntity.resource_pool == resource_pool)
        .where(TaskUserScheduleEntity.user_id == user_id)
        .with_for_update()
    )
    schedule = result.first()
    if schedule is None:
        raise RuntimeError("Task user schedule scope disappeared after creation.")
    return schedule


def _available_slot_filter(quota: TaskQuotaEntity):
    if quota.running_count < quota.max_concurrency:
        return None
    return TaskRunEntity.quota_slot_released.is_(False)


async def _resource_eligible_run_ids(
    session: Any,
    *,
    service: str,
    tenant_id: str,
    resource_pool: str,
    now: datetime,
) -> set[str] | None:
    """Apply read/write resource ordering before user-level fair scheduling."""

    if resource_pool == "default":
        return None

    result = await session.exec(
        select(TaskRunEntity, TaskEntity)
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
            | (TaskRunEntity.lease_until <= now)
        )
    )
    rows = list(result.all())
    legacy_ids = {
        row[0].id for row in rows if row[0].resource_access_mode is None
    }
    resource_rows = [
        (row[0], row[1])
        for row in rows
        if row[0].resource_access_mode in {"read", "write"}
    ]
    if not resource_rows:
        return legacy_ids

    active_result = await session.exec(
        select(TaskRunEntity.resource_access_mode)
        .join(TaskEntity, TaskEntity.id == TaskRunEntity.task_id)
        .where(TaskRunEntity.status == "running")
        .where(TaskRunEntity.resource_pool == resource_pool)
        .where(TaskRunEntity.resource_access_mode.is_not(None))
        .where(TaskRunEntity.lease_until > now)
        .where(TaskEntity.service == service)
        .where(TaskEntity.tenant_id == tenant_id)
        .where(TaskEntity.status == "running")
        .where(TaskEntity.current_run_id == TaskRunEntity.id)
    )
    active_modes = {str(value) for value in active_result.all()}
    if "write" in active_modes:
        return legacy_ids

    def resource_key(item):
        run, task = item
        retry_rank = int(
            run.resource_access_mode == "read" and run.lease_version > 0
        )
        return retry_rank, task.created_at, task.id

    readers = sorted(
        (item for item in resource_rows if item[0].resource_access_mode == "read"),
        key=resource_key,
    )
    writers = sorted(
        (item for item in resource_rows if item[0].resource_access_mode == "write"),
        key=resource_key,
    )
    first_writer_key = resource_key(writers[0]) if writers else None
    readers_before_writer = [
        item
        for item in readers
        if first_writer_key is None or resource_key(item) < first_writer_key
    ]

    eligible = set(legacy_ids)
    if "read" in active_modes:
        eligible.update(item[0].id for item in readers_before_writer)
    elif readers_before_writer:
        eligible.update(item[0].id for item in readers_before_writer)
    elif writers:
        eligible.add(writers[0][0].id)
    else:
        eligible.update(item[0].id for item in readers)
    return eligible


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

    quota_result = await session.exec(
        select(
            TaskQuotaEntity.service,
            TaskQuotaEntity.tenant_id,
            TaskQuotaEntity.resource_pool,
            TaskQuotaEntity.last_scheduled_at,
        )
    )
    quota_by_key = {
        (row[0], row[1], row[2]): row[3]
        for row in quota_result.all()
    }

    def fair_key(item):
        last_scheduled_at = quota_by_key.get((item[0], item[1], item[2]))
        return (
            last_scheduled_at or datetime.min,
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
        now = utc_now()
        expired_result = await session.exec(
            select(TaskRunEntity)
            .join(TaskEntity, TaskEntity.id == TaskRunEntity.task_id)
            .where(TaskRunEntity.status == "running")
            .where(TaskRunEntity.quota_slot_released.is_(False))
            .where(TaskRunEntity.lease_until <= now)
            .where(TaskRunEntity.resource_pool == resource_pool)
            .where(TaskEntity.service == service)
            .where(TaskEntity.tenant_id == tenant_id)
            .where(TaskEntity.current_run_id == TaskRunEntity.id)
            .with_for_update(skip_locked=True)
        )
        expired_runs = list(expired_result.all())
        for expired_run in expired_runs:
            expired_run.quota_slot_released = True
            session.add(expired_run)
        if expired_runs:
            quota.running_count = max(0, quota.running_count - len(expired_runs))

        eligible_run_ids = await _resource_eligible_run_ids(
            session,
            service=service,
            tenant_id=tenant_id,
            resource_pool=resource_pool,
            now=now,
        )
        if eligible_run_ids is not None and not eligible_run_ids:
            continue
        slot_filter = _available_slot_filter(quota)
        user_statement = (
            select(
                TaskEntity.user_id,
                func.min(TaskEntity.created_at).label("oldest_created_at"),
                TaskUserScheduleEntity.last_scheduled_at,
            )
            .join(TaskRunEntity, TaskRunEntity.task_id == TaskEntity.id)
            .outerjoin(
                TaskUserScheduleEntity,
                and_(
                    TaskUserScheduleEntity.service == TaskEntity.service,
                    TaskUserScheduleEntity.tenant_id == TaskEntity.tenant_id,
                    TaskUserScheduleEntity.resource_pool == TaskRunEntity.resource_pool,
                    TaskUserScheduleEntity.user_id == TaskEntity.user_id,
                ),
            )
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
            .group_by(TaskEntity.user_id, TaskUserScheduleEntity.last_scheduled_at)
            .order_by(
                TaskUserScheduleEntity.last_scheduled_at.asc().nullsfirst(),
                func.min(TaskEntity.created_at),
                TaskEntity.user_id,
            )
            .limit(1)
        )
        if eligible_run_ids is not None:
            user_statement = user_statement.where(
                TaskRunEntity.id.in_(sorted(eligible_run_ids))
            )
        if slot_filter is not None:
            user_statement = user_statement.where(slot_filter)
        user_result = await session.exec(user_statement)
        user_candidate = user_result.first()
        if user_candidate is None:
            continue
        user_id = user_candidate[0]
        user_schedule = await lock_user_schedule_scope(
            session,
            service=service,
            tenant_id=tenant_id,
            resource_pool=resource_pool,
            user_id=user_id,
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
            .where(TaskEntity.user_id == user_id)
            .where(TaskRunEntity.resource_pool == resource_pool)
            .where(
                (TaskRunEntity.lease_until.is_(None))
                | (TaskRunEntity.lease_until <= utc_now())
            )
            .with_for_update(skip_locked=True)
        )
        if eligible_run_ids is not None:
            statement = statement.where(
                TaskRunEntity.id.in_(sorted(eligible_run_ids))
            )
        if slot_filter is not None:
            statement = statement.where(slot_filter)
        if resource_pool == "default":
            statement = statement.order_by(
                TaskEntity.priority.desc(), TaskEntity.created_at, TaskEntity.id
            )
        else:
            statement = statement.order_by(
                case(
                    (
                        and_(
                            TaskRunEntity.resource_access_mode == "read",
                            TaskRunEntity.lease_version > 0,
                        ),
                        1,
                    ),
                    else_=0,
                ),
                TaskEntity.priority.desc(),
                TaskEntity.created_at,
                TaskEntity.id,
            )
        statement = statement.limit(1)
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
        user_schedule.last_scheduled_at = now
        user_schedule.updated_at = now
        session.add(run)
        session.add(quota)
        session.add(user_schedule)
        await session.commit()
        return task.id, run.id, run.lease_version
    return None


async def describe_resource_wait(
    session: Any,
    run: TaskRunEntity,
) -> tuple[str | None, int]:
    """Describe a resource Run without changing its durable queue state."""

    mode = run.resource_access_mode
    if mode not in {"read", "write"}:
        return None, 0
    if run.status not in {"pending", "running"}:
        return run.status.upper(), 0
    now = utc_now()
    if run.lease_owner and run.lease_until and run.lease_until > now:
        return "RUNNING", 0
    task = await session.get(TaskEntity, run.task_id)
    if task is None:
        return "WAITING_RESOURCE", 0
    active_readers = (
        await session.exec(
            select(func.count())
            .select_from(TaskRunEntity)
            .join(TaskEntity, TaskEntity.id == TaskRunEntity.task_id)
            .where(TaskRunEntity.status == "running")
            .where(TaskRunEntity.id != run.id)
            .where(TaskRunEntity.resource_pool == run.resource_pool)
            .where(TaskRunEntity.resource_access_mode == "read")
            .where(TaskRunEntity.lease_until > now)
            .where(TaskEntity.service == task.service)
            .where(TaskEntity.tenant_id == task.tenant_id)
            .where(TaskEntity.status == "running")
            .where(TaskEntity.current_run_id == TaskRunEntity.id)
        )
    ).one()
    earlier_writer = (
        await session.exec(
            select(func.count())
            .select_from(TaskRunEntity)
            .join(TaskEntity, TaskEntity.id == TaskRunEntity.task_id)
            .where(TaskRunEntity.status == "running")
            .where(TaskRunEntity.id != run.id)
            .where(TaskRunEntity.cancel_requested.is_(False))
            .where(TaskRunEntity.resource_pool == run.resource_pool)
            .where(TaskRunEntity.resource_access_mode == "write")
            .where(
                (TaskRunEntity.lease_until.is_(None))
                | (TaskRunEntity.lease_until <= now)
            )
            .where(TaskEntity.service == task.service)
            .where(TaskEntity.tenant_id == task.tenant_id)
            .where(TaskEntity.status == "running")
            .where(TaskEntity.cancel_requested.is_(False))
            .where(TaskEntity.current_run_id == TaskRunEntity.id)
            .where(
                or_(
                    TaskEntity.created_at < task.created_at,
                    and_(
                        TaskEntity.created_at == task.created_at,
                        TaskEntity.id < task.id,
                    ),
                )
            )
        )
    ).one()
    if mode == "write" and int(active_readers or 0) > 0:
        return "WAITING_RESOURCE", int(active_readers)
    if mode == "read" and int(earlier_writer or 0) > 0:
        return "WAITING_RESOURCE", 0
    return "QUEUED", 0


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
        sync_limit=False,
    )
    quota.running_count = max(0, quota.running_count - 1)
    quota.updated_at = utc_now()
    session.add(quota)
