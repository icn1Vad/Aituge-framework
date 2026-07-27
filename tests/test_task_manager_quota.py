from __future__ import annotations

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskEntity, TaskQuotaEntity
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

    async def create_started(tenant_id: str, goal: str):
        task = await service.create_task(
            TaskCreateRequest(
                task_type="pipeline.demo",
                input_payload={"goal": goal},
                user_id=f"{tenant_id}-user",
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
