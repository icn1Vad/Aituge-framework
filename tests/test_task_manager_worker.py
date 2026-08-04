from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskEntity, TaskQuotaEntity, TaskRunEntity, utc_now
from task_manager.registry import ResourceTaskType, register_task_definition
from task_manager.runtime.quota import describe_resource_wait
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
        assert created.resource_access_mode is None

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
    assert all(event.tenant_id == "event-tenant" for event in events)
    assert all(event.user_id == "event-user" for event in events)
    assert all(event.service_name == "ai-contract" for event in events)


@pytest.mark.asyncio
async def test_resource_task_inheritance_is_frozen_on_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'resource-type.db'}")
    reset_engine_for_test()
    await init_db()
    register_task_definition(
        ResourceTaskType(
            task_type="proof.test.read.persisted",
            name="Persisted Proof reader",
            handler="pipeline",
            pipeline_id="pipeline-demo-v1",
            resource_pool="proof-library",
            access_mode="read",
        ),
        source="test-resource-task",
    )
    service = TaskManagerService(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    )
    task = await service.create_task(
        TaskCreateRequest(
            task_type="proof.test.read.persisted",
            input_payload={"goal": "persist resource semantics"},
            tenant_id="tenant-1",
        ),
        service_name="ai-proof",
    )
    run = await service.start_task_run(task.id, TaskRunRequest())

    assert run.resource_pool == "proof-library"
    assert run.resource_access_mode == "read"


@pytest.mark.asyncio
async def test_worker_does_not_claim_external_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'external.db'}")
    reset_engine_for_test()
    await init_db()

    async with create_db_session() as session:
        task = TaskEntity(
            id="external-task",
            task_type="pipeline.demo",
            handler_name="external",
            status="running",
            current_run_id="external-run",
            user_id="external-user",
            tenant_id="external-tenant",
        )
        run = TaskRunEntity(
            id="external-run",
            task_id=task.id,
            status="running",
            started_at=utc_now(),
        )
        session.add(task)
        session.add(run)
        await session.commit()

    worker = TaskWorker(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts"),
        worker_id="worker-external-skip",
    )
    assert await worker.claim_one() is None


@pytest.mark.asyncio
async def test_resource_runs_batch_readers_then_prefer_waiting_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'resource.db'}")
    reset_engine_for_test()
    await init_db()
    base = utc_now()

    async with create_db_session() as session:
        for index, (task_id, mode) in enumerate(
            (
                ("reader-1", "read"),
                ("reader-2", "read"),
                ("writer-1", "write"),
                ("reader-late", "read"),
            )
        ):
            run_id = f"{task_id}-run"
            task = TaskEntity(
                id=task_id,
                service="ai-proof",
                task_type="proof.test",
                handler_name="pipeline",
                status="running",
                current_run_id=run_id,
                user_id="user",
                tenant_id="tenant-1",
                created_at=base + timedelta(milliseconds=index),
            )
            run = TaskRunEntity(
                id=run_id,
                task_id=task_id,
                status="running",
                resource_pool="proof-library",
                resource_access_mode=mode,
                started_at=task.created_at,
                created_at=task.created_at,
            )
            session.add(task)
            session.add(run)
        await session.commit()

    worker = TaskWorker(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts"),
        worker_id="resource-worker",
        tenant_concurrency=4,
    )
    first = await worker.claim_one()
    second = await worker.claim_one()
    assert first and first.task_id == "reader-1"
    assert second and second.task_id == "reader-2"
    assert await worker.claim_one() is None

    async with create_db_session() as session:
        writer_run = await session.get(TaskRunEntity, "writer-1-run")
        late_reader_run = await session.get(TaskRunEntity, "reader-late-run")
        assert await describe_resource_wait(session, writer_run) == (
            "WAITING_RESOURCE",
            2,
        )
        assert await describe_resource_wait(session, late_reader_run) == (
            "WAITING_RESOURCE",
            0,
        )

    async with create_db_session() as session:
        for run_id in ("reader-1-run", "reader-2-run"):
            run = await session.get(TaskRunEntity, run_id)
            task = await session.get(TaskEntity, run.task_id)
            run.status = "succeeded"
            run.lease_owner = None
            run.lease_until = base - timedelta(seconds=1)
            task.status = "succeeded"
            session.add(run)
            session.add(task)
        await session.commit()

    writer = await worker.claim_one()
    assert writer and writer.task_id == "writer-1"
    assert await worker.claim_one() is None


@pytest.mark.asyncio
async def test_resource_runs_are_isolated_by_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tenants.db'}")
    reset_engine_for_test()
    await init_db()

    async with create_db_session() as session:
        for tenant_id, task_id, mode in (
            ("tenant-1", "tenant-1-reader", "read"),
            ("tenant-2", "tenant-2-writer", "write"),
        ):
            run_id = f"{task_id}-run"
            task = TaskEntity(
                id=task_id,
                service="ai-proof",
                task_type="proof.test",
                handler_name="pipeline",
                status="running",
                current_run_id=run_id,
                tenant_id=tenant_id,
            )
            session.add(task)
            session.add(
                TaskRunEntity(
                    id=run_id,
                    task_id=task_id,
                    status="running",
                    resource_pool="proof-library",
                    resource_access_mode=mode,
                )
            )
        await session.commit()

    worker = TaskWorker(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts"),
        worker_id="tenant-worker",
        tenant_concurrency=2,
    )
    claims = [await worker.claim_one(), await worker.claim_one()]
    assert {claim.task_id for claim in claims if claim} == {
        "tenant-1-reader",
        "tenant-2-writer",
    }


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("FRAMEWORK_POSTGRES_CONCURRENCY_TEST") != "1",
    reason="requires an explicitly configured temporary PostgreSQL database",
)
async def test_five_tenant_admin_review_runs_claim_concurrently(tmp_path):
    """Five tenant administrators coordinate reads and writes independently."""

    reset_engine_for_test()
    await init_db()
    register_task_definition(
        ResourceTaskType(
            task_type="proof.audit.run",
            name="Five-tenant Proof audit",
            handler="pipeline",
            # Claim coordination is independent of stage execution; the built-in
            # test pipeline keeps this PostgreSQL test focused on durable leases.
            pipeline_id="pipeline-demo-v1",
            resource_pool="proof-library",
            access_mode="read",
        ),
        source="test-five-tenant-proof-audit",
    )
    register_task_definition(
        ResourceTaskType(
            task_type="proof.policy.mutate",
            name="Five-tenant Proof lifecycle mutation",
            handler="pipeline",
            pipeline_id="pipeline-demo-v1",
            resource_pool="proof-library",
            access_mode="write",
        ),
        source="test-five-tenant-proof-mutation",
    )
    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    expected_reads: dict[str, tuple[str, str]] = {}
    expected_writes: dict[str, tuple[str, str]] = {}
    for index in range(1, 6):
        tenant_id = str(index)
        user_id = f"tenant-{index}-admin"
        read_task = await service.create_task(
            TaskCreateRequest(
                task_type="proof.audit.run",
                input_payload={"policy_id": f"policy-{index}"},
                user_id=user_id,
                tenant_id=tenant_id,
            ),
            service_name="ai-proof",
        )
        read_run = await service.start_task_run(
            read_task.id,
            TaskRunRequest(idempotency_key=f"tenant-{index}-audit-run"),
        )
        expected_reads[read_task.id] = (tenant_id, read_run.id)
        write_task = await service.create_task(
            TaskCreateRequest(
                task_type="proof.policy.mutate",
                input_payload={
                    "policy_id": f"policy-{index}",
                    "action": "expire",
                },
                user_id=user_id,
                tenant_id=tenant_id,
            ),
            service_name="ai-proof",
        )
        write_run = await service.start_task_run(
            write_task.id,
            TaskRunRequest(idempotency_key=f"tenant-{index}-mutation-run"),
        )
        expected_writes[write_task.id] = (tenant_id, write_run.id)

    workers = [
        TaskWorker(
            options,
            worker_id=f"five-tenant-worker-{index}",
            tenant_concurrency=4,
        )
        for index in range(1, 6)
    ]
    claims = await asyncio.gather(*(worker.claim_one() for worker in workers))

    assert all(claim is not None for claim in claims)
    assert {claim.task_id for claim in claims if claim} == set(expected_reads)
    async with create_db_session() as session:
        for claim in claims:
            assert claim is not None
            task = await session.get(TaskEntity, claim.task_id)
            run = await session.get(TaskRunEntity, claim.run_id)
            tenant_id, run_id = expected_reads[claim.task_id]
            assert task is not None
            assert run is not None
            assert task.tenant_id == tenant_id
            assert task.user_id == f"tenant-{tenant_id}-admin"
            assert run.id == run_id
            assert run.resource_pool == "proof-library"
            assert run.resource_access_mode == "read"
            assert run.lease_owner == claim.owner
        for task_id, (tenant_id, run_id) in expected_writes.items():
            write_run = await session.get(TaskRunEntity, run_id)
            assert write_run is not None
            assert await describe_resource_wait(session, write_run) == (
                "WAITING_RESOURCE",
                1,
            )

    blocked_writes = await asyncio.gather(
        *(worker.claim_one() for worker in workers)
    )
    assert blocked_writes == [None] * 5

    async with create_db_session() as session:
        for task_id, (_tenant_id, run_id) in expected_reads.items():
            task = await session.get(TaskEntity, task_id)
            run = await session.get(TaskRunEntity, run_id)
            assert task is not None
            assert run is not None
            task.status = "succeeded"
            run.status = "succeeded"
            run.lease_owner = None
            run.lease_until = utc_now() - timedelta(seconds=1)
            session.add(task)
            session.add(run)
        await session.commit()

    write_claims = await asyncio.gather(
        *(worker.claim_one() for worker in workers)
    )
    assert all(claim is not None for claim in write_claims)
    assert {claim.task_id for claim in write_claims if claim} == set(expected_writes)
    async with create_db_session() as session:
        for claim in write_claims:
            assert claim is not None
            task = await session.get(TaskEntity, claim.task_id)
            run = await session.get(TaskRunEntity, claim.run_id)
            tenant_id, run_id = expected_writes[claim.task_id]
            assert task is not None
            assert run is not None
            assert task.tenant_id == tenant_id
            assert run.id == run_id
            assert run.resource_access_mode == "write"
            assert run.lease_owner == claim.owner


@pytest.mark.asyncio
async def test_expired_reader_lease_releases_quota_for_waiting_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'expired-reader.db'}")
    reset_engine_for_test()
    await init_db()
    base = utc_now()

    async with create_db_session() as session:
        reader = TaskEntity(
            id="expired-reader",
            service="ai-proof",
            task_type="proof.test",
            handler_name="pipeline",
            status="running",
            current_run_id="expired-reader-run",
            tenant_id="tenant-1",
            created_at=base,
        )
        writer = TaskEntity(
            id="waiting-writer",
            service="ai-proof",
            task_type="proof.test",
            handler_name="pipeline",
            status="running",
            current_run_id="waiting-writer-run",
            tenant_id="tenant-1",
            created_at=base + timedelta(milliseconds=1),
        )
        session.add(reader)
        session.add(writer)
        session.add(
            TaskRunEntity(
                id="expired-reader-run",
                task_id=reader.id,
                status="running",
                resource_pool="proof-library",
                resource_access_mode="read",
                lease_owner="lost-worker",
                lease_until=base - timedelta(seconds=1),
                lease_version=1,
                quota_slot_released=False,
            )
        )
        session.add(
            TaskRunEntity(
                id="waiting-writer-run",
                task_id=writer.id,
                status="running",
                resource_pool="proof-library",
                resource_access_mode="write",
            )
        )
        session.add(
            TaskQuotaEntity(
                service="ai-proof",
                tenant_id="tenant-1",
                resource_pool="proof-library",
                max_concurrency=1,
                running_count=1,
            )
        )
        await session.commit()

    worker = TaskWorker(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts"),
        worker_id="writer-worker",
        tenant_concurrency=1,
    )
    claimed = await worker.claim_one()

    assert claimed and claimed.task_id == "waiting-writer"


@pytest.mark.asyncio
async def test_worker_fails_unregistered_task_and_releases_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'unsupported.db'}")
    reset_engine_for_test()
    await init_db()

    async with create_db_session() as session:
        task = TaskEntity(
            id="unsupported-task",
            task_type="contract.review.unregistered",
            handler_name="pipeline",
            status="running",
            current_run_id="unsupported-run",
            user_id="unsupported-user",
            tenant_id="unsupported-tenant",
            service="ai-contract",
        )
        run = TaskRunEntity(
            id="unsupported-run",
            task_id=task.id,
            status="running",
            started_at=utc_now(),
        )
        session.add(task)
        session.add(run)
        await session.commit()

    worker = TaskWorker(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts"),
        worker_id="unsupported-worker",
    )
    assert await worker.run_once() is True

    async with create_db_session() as session:
        task = await session.get(TaskEntity, "unsupported-task")
        run = await session.get(TaskRunEntity, "unsupported-run")
        quota = (
            await session.exec(
                select(TaskQuotaEntity).where(
                    TaskQuotaEntity.service == "ai-contract",
                    TaskQuotaEntity.tenant_id == "unsupported-tenant",
                    TaskQuotaEntity.resource_pool == "default",
                )
            )
        ).one()
        assert task is not None
        assert run is not None
        assert task.status == "failed"
        assert task.error_payload_json["type"] == "ValueError"
        assert run.status == "failed"
        assert run.error_code == "ValueError"
        assert run.quota_slot_released is True
        assert quota.running_count == 0

    assert await worker.claim_one() is None
