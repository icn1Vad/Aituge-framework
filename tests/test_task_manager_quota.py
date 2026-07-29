from __future__ import annotations

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import (
    TaskEntity,
    TaskQuotaEntity,
    TaskRunEntity,
    TaskUserScheduleEntity,
    utc_now,
)
from task_manager.runtime.worker import TaskWorker
from task_manager.schemas import TaskCreateRequest
from task_manager.service import TaskManagerService


@pytest.mark.asyncio
async def test_fair_worker_respects_tenant_slots_and_releases_them(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "SQLITE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'quota.db'}",
    )
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)

    async def create_started(tenant_id: str, goal: str, user_id: str | None = None):
        task = await service.create_task(
            TaskCreateRequest(
                task_type="pipeline.demo",
                input_payload={"goal": goal},
                user_id=user_id or f"{tenant_id}-user",
                tenant_id=tenant_id,
            ),
            service_name="ai-contract",
        )
        run = await service.start_task_run(task.id)
        return task, run

    tenant_a_first, _ = await create_started("tenant-a", "a-first")
    tenant_a_second, _ = await create_started("tenant-a", "a-second")
    tenant_b_first, _ = await create_started("tenant-b", "b-first")

    worker = TaskWorker(
        options,
        worker_id="fair-worker",
        lease_seconds=30,
        tenant_concurrency=1,
    )

    first_lease = await worker.claim_one()
    assert first_lease is not None
    assert first_lease.task_id == tenant_a_first.id

    async with create_db_session() as session:
        quota = (
            await session.exec(
                select(TaskQuotaEntity)
                .where(TaskQuotaEntity.service == "ai-contract")
                .where(TaskQuotaEntity.tenant_id == "tenant-a")
            )
        ).one()
        assert quota.running_count == 1

    second_lease = await worker.claim_one()
    assert second_lease is not None
    assert second_lease.task_id == tenant_b_first.id

    await service._finish_task(tenant_a_first.id, status="failed", outcome="test")
    await service._finish_task(tenant_b_first.id, status="failed", outcome="test")

    async with create_db_session() as session:
        quotas = (
            await session.exec(
                select(TaskQuotaEntity)
                .order_by(TaskQuotaEntity.tenant_id)
            )
        ).all()
        assert [(row.tenant_id, row.running_count) for row in quotas] == [
            ("tenant-a", 0),
            ("tenant-b", 0),
        ]

    third_lease = await worker.claim_one()
    assert third_lease is not None
    assert third_lease.task_id == tenant_a_second.id
    await service._finish_task(tenant_a_second.id, status="failed", outcome="test")

    async with create_db_session() as session:
        quotas = (
            await session.exec(
                select(TaskQuotaEntity)
                .order_by(TaskQuotaEntity.tenant_id)
            )
        ).all()
        assert all(row.running_count == 0 for row in quotas)
        task_rows = (
            await session.exec(
                select(TaskEntity).where(TaskEntity.tenant_id == "tenant-a")
            )
        ).all()
        assert len(task_rows) == 2


@pytest.mark.asyncio
async def test_fair_worker_rotates_users_before_applying_user_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'user-fair.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)

    async def create_started(user_id: str, goal: str, priority: int):
        task = await service.create_task(
            TaskCreateRequest(
                task_type="pipeline.demo",
                input_payload={"goal": goal},
                user_id=user_id,
                tenant_id="tenant-a",
                priority=priority,
            ),
            service_name="ai-contract",
        )
        await service.start_task_run(task.id)
        return task

    user_a_high = await create_started("user-a", "a-high", 100)
    user_a_low = await create_started("user-a", "a-low", 0)
    user_b = await create_started("user-b", "b-normal", 0)
    worker = TaskWorker(options, worker_id="user-fair-worker", tenant_concurrency=10)

    first = await worker.claim_one()
    second = await worker.claim_one()
    third = await worker.claim_one()

    assert first is not None and first.task_id == user_a_high.id
    assert second is not None and second.task_id == user_b.id
    assert third is not None and third.task_id == user_a_low.id

    async with create_db_session() as session:
        schedules = (
            await session.exec(
                select(TaskUserScheduleEntity).order_by(TaskUserScheduleEntity.user_id)
            )
        ).all()
        assert [row.user_id for row in schedules] == ["user-a", "user-b"]
        quota = (await session.exec(select(TaskQuotaEntity))).one()
        assert quota.max_concurrency == 10
        assert quota.running_count == 3

    for task in (user_a_high, user_a_low, user_b):
        await service._finish_task(task.id, status="failed", outcome="test")


@pytest.mark.asyncio
async def test_quota_configuration_updates_existing_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'quota-update.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)

    async def create_started(goal: str):
        task = await service.create_task(
            TaskCreateRequest(
                task_type="pipeline.demo",
                input_payload={"goal": goal},
                user_id="user-a",
                tenant_id="tenant-a",
            ),
            service_name="ai-contract",
        )
        await service.start_task_run(task.id)
        return task

    first_task = await create_started("first")
    first_worker = TaskWorker(options, worker_id="limit-2", tenant_concurrency=2)
    assert await first_worker.claim_one() is not None
    await service._finish_task(first_task.id, status="failed", outcome="test")

    second_task = await create_started("second")
    second_worker = TaskWorker(options, worker_id="limit-10", tenant_concurrency=10)
    assert await second_worker.claim_one() is not None

    async with create_db_session() as session:
        quota = (await session.exec(select(TaskQuotaEntity))).one()
        assert quota.max_concurrency == 10

    await service._finish_task(second_task.id, status="failed", outcome="test")


@pytest.mark.asyncio
async def test_cancel_unclaimed_run_becomes_terminal_and_cannot_be_claimed(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(
        TaskCreateRequest(
            task_type="pipeline.demo",
            input_payload={"goal": "cancel-before-claim"},
            user_id="user-a",
            tenant_id="tenant-a",
        ),
        service_name="ai-contract",
    )
    run = await service.start_task_run(task.id)

    cancelled = await service.request_cancel(run.id)

    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is True
    worker = TaskWorker(options, worker_id="cancel-worker", tenant_concurrency=10)
    assert await worker.claim_one() is None


@pytest.mark.asyncio
async def test_cancel_active_run_requests_worker_stop_without_releasing_early(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cancel-active.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(
        TaskCreateRequest(
            task_type="pipeline.demo",
            input_payload={"goal": "cancel-active"},
            user_id="user-a",
            tenant_id="tenant-a",
        ),
        service_name="ai-contract",
    )
    run = await service.start_task_run(task.id)
    worker = TaskWorker(options, worker_id="active-worker", tenant_concurrency=10)
    lease = await worker.claim_one()
    assert lease is not None

    cancelling = await service.request_cancel(run.id)

    assert cancelling.status == "running"
    assert cancelling.cancel_requested is True
    async with create_db_session() as session:
        quota = (await session.exec(select(TaskQuotaEntity))).one()
        assert quota.running_count == 1
    await service._finish_task(task.id, status="cancelled", outcome="cancelled")
    async with create_db_session() as session:
        quota = (await session.exec(select(TaskQuotaEntity))).one()
        assert quota.running_count == 0


@pytest.mark.asyncio
async def test_cancel_expired_run_releases_quota_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cancel-expired.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(
        TaskCreateRequest(
            task_type="pipeline.demo",
            input_payload={"goal": "cancel-expired"},
            user_id="user-a",
            tenant_id="tenant-a",
        ),
        service_name="ai-contract",
    )
    run = await service.start_task_run(task.id)
    worker = TaskWorker(options, worker_id="expired-worker", tenant_concurrency=10)
    lease = await worker.claim_one()
    assert lease is not None
    async with create_db_session() as session:
        row = await session.get(TaskRunEntity, run.id)
        assert row is not None
        row.lease_until = utc_now()
        session.add(row)
        await session.commit()

    cancelled = await service.request_cancel(run.id)
    repeated = await service.request_cancel(run.id)

    assert cancelled.status == "cancelled"
    assert repeated.status == "cancelled"
    async with create_db_session() as session:
        quota = (await session.exec(select(TaskQuotaEntity))).one()
        assert quota.running_count == 0
