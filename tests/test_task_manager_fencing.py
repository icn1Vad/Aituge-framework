from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskRunEntity, utc_now
from task_manager.pipeline.store import (
    create_artifact,
    create_stage_run,
    update_run,
    update_stage_run,
)
from task_manager.runtime.fencing import (
    ExecutionLease,
    RunLeaseLost,
    bind_execution_lease,
)
from task_manager.runtime.worker import RunLease, TaskWorker
from task_manager.schemas import TaskCreateRequest, TaskRunRequest
from task_manager.service import TaskManagerService


@pytest.mark.asyncio
async def test_execution_fencing_blocks_worker_writes_after_lease_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///${tmp_path / 'fencing.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(
        TaskCreateRequest(
            task_type="pipeline.demo",
            input_payload={"goal": "fencing"},
            user_id="fencing-user",
            tenant_id="fencing-tenant",
        ),
        service_name="ai-contract",
    )
    run = await service.start_task_run(task.id, TaskRunRequest(idempotency_key="fencing-run"))
    worker = TaskWorker(options, worker_id="fencing-worker", lease_seconds=30)
    claimed = await worker.claim_one()
    assert claimed is not None
    assert claimed.run_id == run.id

    lease = ExecutionLease(run_id=claimed.run_id, owner=claimed.owner, version=claimed.version)
    with bind_execution_lease(lease):
        await service.record_event(
            task_id=task.id,
            run_id=run.id,
            event_type="before_loss",
            stage="test",
            message="write is accepted while the lease is valid",
        )
        await update_run(run.id, current_stage_id="analyze")
        stage_run = await create_stage_run(
            task_id=task.id,
            run_id=run.id,
            stage_id="analyze",
            stage_type="deterministic",
            attempt=1,
            agent_id=None,
            input_artifact_ids=[],
        )
        await update_stage_run(stage_run.id, status="succeeded")
        await create_artifact(
            task_id=task.id,
            run_id=run.id,
            stage_run_id=stage_run.id,
            artifact_type="fencing_result",
            schema_name="",
            content={"ok": True},
            parent_artifact_ids=[],
        )

        lease.mark_lost("test heartbeat failure")
        with pytest.raises(RunLeaseLost):
            await service.record_event(
                task_id=task.id,
                run_id=run.id,
                event_type="after_loss",
                stage="test",
                message="write must be rejected",
            )
        with pytest.raises(RunLeaseLost):
            await update_run(run.id, current_stage_id="stale")
        with pytest.raises(RunLeaseLost):
            await update_stage_run(stage_run.id, status="failed")
        with pytest.raises(RunLeaseLost):
            await create_stage_run(
                task_id=task.id,
                run_id=run.id,
                stage_id="stale",
                stage_type="deterministic",
                attempt=1,
                agent_id=None,
                input_artifact_ids=[],
            )
        with pytest.raises(RunLeaseLost):
            await create_artifact(
                task_id=task.id,
                run_id=run.id,
                stage_run_id=stage_run.id,
                artifact_type="stale_result",
                schema_name="",
                content={"ok": False},
                parent_artifact_ids=[],
            )
        with pytest.raises(RunLeaseLost):
            await service._finish_task(task.id, status="succeeded", result={"stale": True})

    events = await service.list_run_events(run.id, limit=20)
    assert [event.event_type for event in events] == ["before_loss"]
    async with create_db_session() as session:
        stage_rows = list(
            (
                await session.exec(
                    select(TaskRunEntity).where(TaskRunEntity.id == run.id)
                )
            ).all()
        )
        assert len(stage_rows) == 1
        assert stage_rows[0].current_stage_id == "analyze"
    assert await worker.release_lease(claimed) is True


@pytest.mark.asyncio
async def test_execution_fencing_rejects_stale_database_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///${tmp_path / 'stale-fencing.db'}")
    reset_engine_for_test()
    await init_db()

    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(
        TaskCreateRequest(
            task_type="pipeline.demo",
            input_payload={"goal": "stale fencing"},
            user_id="stale-user",
            tenant_id="stale-tenant",
        ),
        service_name="ai-contract",
    )
    run = await service.start_task_run(task.id, TaskRunRequest())
    first_worker = TaskWorker(options, worker_id="stale-a", lease_seconds=30)
    first = await first_worker.claim_one()
    assert first is not None

    async with create_db_session() as session:
        row = await session.get(TaskRunEntity, run.id)
        assert row is not None
        row.lease_until = utc_now()
        session.add(row)
        await session.commit()

    second_worker = TaskWorker(options, worker_id="stale-b", lease_seconds=30)
    second = await second_worker.claim_one()
    assert second is not None
    assert second.version == first.version + 1

    with bind_execution_lease(
        ExecutionLease(run_id=first.run_id, owner=first.owner, version=first.version)
    ):
        with pytest.raises(RunLeaseLost):
            await service.record_event(
                task_id=task.id,
                run_id=run.id,
                event_type="stale_owner",
                stage="test",
                message="stale owner must not write",
            )

    assert await second_worker.release_lease(second) is True


@pytest.mark.asyncio
async def test_heartbeat_marks_execution_lease_lost_after_two_failures(tmp_path, monkeypatch):
    worker = TaskWorker(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts"),
        worker_id="heartbeat-worker",
        lease_seconds=30,
        heartbeat_seconds=1,
    )
    worker.heartbeat_seconds = 0.001
    lease = RunLease(task_id="task", run_id="run", owner="heartbeat-worker", version=1)
    execution_lease = ExecutionLease(run_id="run", owner="heartbeat-worker", version=1)

    async def failed_renewal(_lease):
        return False

    monkeypatch.setattr(worker, "renew_lease", failed_renewal)
    await worker._heartbeat_loop(lease, asyncio.Event(), execution_lease)

    assert execution_lease.lost is True
    assert "two consecutive" in execution_lease.loss_reason

