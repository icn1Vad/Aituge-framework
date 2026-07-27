from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskRunEntity, utc_now
from task_manager.runtime.worker import TaskWorker
from task_manager.schemas import TaskCreateRequest, TaskRunRequest
from task_manager.service import TaskManagerService


def test_worker_claims_renews_and_recovers_a_persisted_run(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}",
        )
        reset_engine_for_test()
        await init_db()
        options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
        service = TaskManagerService(options)
        task = await service.create_task(
            TaskCreateRequest(
                task_type="pipeline.demo",
                input_payload={"goal": "worker claim"},
                user_id="worker-user",
                tenant_id="worker-tenant",
            ),
            service_name="ai-contract",
        )
        created = await service.start_task_run(task.id, TaskRunRequest(idempotency_key="worker-run"))

        first_worker = TaskWorker(
            options,
            worker_id="worker-a",
            lease_seconds=30,
            heartbeat_seconds=5,
        )
        first_lease = await first_worker.claim_one()
        assert first_lease is not None
        assert first_lease.run_id == created.id
        assert first_lease.owner == "worker-a"
        assert first_lease.version == 1
        assert await first_worker.renew_lease(first_lease) is True

        async with create_db_session() as session:
            run_row = (
                await session.exec(
                    select(TaskRunEntity).where(TaskRunEntity.id == created.id)
                )
            ).one()
            run_row.lease_until = utc_now()
            session.add(run_row)
            await session.commit()

        second_worker = TaskWorker(options, worker_id="worker-b", lease_seconds=30)
        second_lease = await second_worker.claim_one()
        assert second_lease is not None
        assert second_lease.run_id == created.id
        assert second_lease.owner == "worker-b"
        assert second_lease.version == 2
        assert await first_worker.renew_lease(first_lease) is False
        assert await second_worker.release_lease(second_lease) is True

    asyncio.run(run())


@pytest.mark.asyncio
async def test_run_event_sequences_are_allocated_from_the_run_row(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "SQLITE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'events.db'}",
    )
    reset_engine_for_test()
    await init_db()
    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(
        TaskCreateRequest(
            task_type="pipeline.demo",
            input_payload={"goal": "event sequence"},
            user_id="event-user",
            tenant_id="event-tenant",
        ),
        service_name="ai-contract",
    )
    run = await service.start_task_run(task.id, TaskRunRequest())

    for index in range(4):
        await service.record_event(
            task_id=task.id,
            run_id=run.id,
            event_type="test_event",
            stage="test",
            message=str(index),
        )
    events = await service.list_run_events(run.id, limit=20)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
