from __future__ import annotations

import asyncio
import os
from collections import Counter

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskEntity, TaskQuotaEntity, TaskRunEntity
from task_manager.runtime.worker import TaskWorker
from task_manager.schemas import TaskCreateRequest
from task_manager.service import TaskManagerService


POSTGRES_TEST_ENABLED = os.getenv("TASK_MANAGER_POSTGRES_TEST_ENABLED") == "1"


@pytest.mark.skipif(not POSTGRES_TEST_ENABLED, reason="PostgreSQL concurrency test is disabled")
@pytest.mark.asyncio
async def test_twenty_workers_respect_tenant_limits_and_rotate_users(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "postgresql")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    tasks: list[TaskEntity] = []
    for tenant_id in ("tenant-a", "tenant-b"):
        for index in range(20):
            user_id = f"{tenant_id}-user-{index % 2}"
            task = await service.create_task(
                TaskCreateRequest(
                    task_type="pipeline.demo",
                    input_payload={"goal": f"{tenant_id}-{index}"},
                    user_id=user_id,
                    tenant_id=tenant_id,
                    priority=index % 3,
                ),
                service_name="ai-contract",
            )
            await service.start_task_run(task.id)
            tasks.append(task)

    workers = [
        TaskWorker(
            options,
            worker_id=f"postgres-worker-{index}",
            tenant_concurrency=10,
        )
        for index in range(20)
    ]
    leases = await asyncio.gather(*(worker.claim_one() for worker in workers))
    claimed = [lease for lease in leases if lease is not None]

    assert len(claimed) == 20
    assert len({lease.run_id for lease in claimed}) == 20
    task_by_id = {task.id: task for task in tasks}
    tenant_counts = Counter(task_by_id[lease.task_id].tenant_id for lease in claimed)
    user_counts = Counter(
        (task_by_id[lease.task_id].tenant_id, task_by_id[lease.task_id].user_id)
        for lease in claimed
    )
    assert tenant_counts == {"tenant-a": 10, "tenant-b": 10}
    assert user_counts == {
        ("tenant-a", "tenant-a-user-0"): 5,
        ("tenant-a", "tenant-a-user-1"): 5,
        ("tenant-b", "tenant-b-user-0"): 5,
        ("tenant-b", "tenant-b-user-1"): 5,
    }

    async with create_db_session() as session:
        quotas = (
            await session.exec(
                select(TaskQuotaEntity).order_by(TaskQuotaEntity.tenant_id)
            )
        ).all()
        assert [(quota.tenant_id, quota.max_concurrency, quota.running_count) for quota in quotas] == [
            ("tenant-a", 10, 10),
            ("tenant-b", 10, 10),
        ]

    for lease in claimed:
        await service._finish_task(lease.task_id, status="failed", outcome="test")

    async with create_db_session() as session:
        quotas = (await session.exec(select(TaskQuotaEntity))).all()
        assert all(quota.running_count == 0 for quota in quotas)

    second_wave = await asyncio.gather(*(worker.claim_one() for worker in workers))
    second_claimed = [lease for lease in second_wave if lease is not None]
    assert len(second_claimed) == 20
    assert not {lease.run_id for lease in claimed} & {lease.run_id for lease in second_claimed}

    for lease in second_claimed:
        await service._finish_task(lease.task_id, status="failed", outcome="test")

    async with create_db_session() as session:
        quotas = (await session.exec(select(TaskQuotaEntity))).all()
        assert all(quota.running_count == 0 for quota in quotas)


@pytest.mark.skipif(not POSTGRES_TEST_ENABLED, reason="PostgreSQL concurrency test is disabled")
@pytest.mark.asyncio
async def test_concurrent_claim_and_cancel_preserves_quota_consistency(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "postgresql")
    reset_engine_for_test()
    await init_db()
    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "race-artifacts")
    service = TaskManagerService(options)
    runs: list[TaskRunEntity] = []
    for index in range(20):
        task = await service.create_task(
            TaskCreateRequest(
                task_type="pipeline.demo",
                input_payload={"goal": f"cancel-race-{index}"},
                user_id=f"race-user-{index % 2}",
                tenant_id="tenant-cancel-race",
            ),
            service_name="ai-contract",
        )
        runs.append(await service.start_task_run(task.id))

    workers = [
        TaskWorker(options, worker_id=f"cancel-worker-{index}", tenant_concurrency=10)
        for index in range(20)
    ]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(worker.claim_one() for worker in workers),
            *(service.request_cancel(run.id) for run in runs),
        ),
        timeout=15,
    )
    claimed = [lease for lease in results[:20] if lease is not None]

    async with create_db_session() as session:
        current_runs = (
            await session.exec(
                select(TaskRunEntity).where(TaskRunEntity.id.in_([run.id for run in runs]))
            )
        ).all()
        quota = (
            await session.exec(
                select(TaskQuotaEntity).where(
                    TaskQuotaEntity.tenant_id == "tenant-cancel-race"
                )
            )
        ).one()
    active_runs = [run for run in current_runs if run.status == "running"]
    assert all(run.cancel_requested for run in current_runs)
    assert all(run.status in {"running", "cancelled"} for run in current_runs)
    assert quota.running_count == len(active_runs) == len(claimed)
    assert len(active_runs) <= 10
    assert await TaskWorker(options, worker_id="after-cancel").claim_one() is None

    for run in active_runs:
        await service._finish_task(run.task_id, status="cancelled", outcome="cancelled")
    async with create_db_session() as session:
        quota = (
            await session.exec(
                select(TaskQuotaEntity).where(
                    TaskQuotaEntity.tenant_id == "tenant-cancel-race"
                )
            )
        ).one()
        assert quota.running_count == 0
