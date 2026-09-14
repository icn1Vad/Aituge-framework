from __future__ import annotations

import asyncio
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import psycopg
from opentelemetry import trace
from loguru import logger
from sqlalchemy import update
from sqlmodel import select

from capability_mount import mount_capabilities_from_env
from extensions.trace.runtime import configure_tracing_from_env
from db.db_context import (
    create_db_session,
    get_database_pool_metrics,
    init_db,
    reset_database_pool_metrics,
)
from backend.observability_integration import assert_observability_schema_if_required
from scheduling.agent_registry import ensure_default_agent_profiles
from scheduling.scheduler import SchedulingRuntimeOptions
from skill import ensure_default_skill_packages
from task_manager.models import TaskEntity, TaskRunEntity, utc_now
from task_manager.runtime.fencing import ExecutionLease, bind_execution_lease
from task_manager.runtime.quota import (
    DEFAULT_TENANT_CONCURRENCY,
    claim_fair_run,
    lock_quota_scope,
)
from task_manager.service import TaskManagerService


@dataclass(frozen=True, slots=True)
class RunLease:
    task_id: str
    run_id: str
    owner: str
    version: int


class LeaseHeartbeatSupervisor:
    """Renew a PostgreSQL Run lease outside the task execution event loop."""

    def __init__(
        self,
        *,
        lease: RunLease,
        execution_lease: ExecutionLease,
        lease_seconds: int,
        heartbeat_seconds: int,
    ) -> None:
        self.lease = lease
        self.execution_lease = execution_lease
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"task-manager-heartbeat:{lease.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=7)
        if self._thread.is_alive():
            self.execution_lease.mark_lost("heartbeat supervisor did not stop cleanly")
            logger.error("TaskManager Run {} heartbeat supervisor did not stop", self.lease.run_id)

    def _run(self) -> None:
        failures = 0
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                if self._renew_once():
                    failures = 0
                    continue
                failures += 1
            except Exception:
                failures += 1
                logger.exception("TaskManager Run {} heartbeat failed", self.lease.run_id)
            if failures >= 2:
                self.execution_lease.mark_lost("two consecutive lease renewal failures")
                logger.error("TaskManager Run {} lost its execution lease", self.lease.run_id)
                return

    def _renew_once(self) -> bool:
        now = utc_now()
        with psycopg.connect(
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "5432")),
            connect_timeout=5,
            application_name=(
                f"{os.environ.get('DB_APPLICATION_NAME', 'framework-worker')}"
                f"-heartbeat:{self.lease.owner}"[:63]
            ),
            autocommit=True,
        ) as conn:
            row = conn.execute(
                """
                UPDATE tuge_task_run
                SET lease_until = %s, last_heartbeat_at = %s, updated_at = %s
                WHERE id = %s AND status = 'running'
                  AND lease_owner = %s AND lease_version = %s
                RETURNING id
                """,
                (
                    now + timedelta(seconds=self.lease_seconds),
                    now,
                    now,
                    self.lease.run_id,
                    self.lease.owner,
                    self.lease.version,
                ),
            ).fetchone()
        return row is not None


class TaskWorker:
    """Persistent PostgreSQL-backed worker for TaskManager Runs."""

    def __init__(
        self,
        options: SchedulingRuntimeOptions,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 120,
        heartbeat_seconds: int = 30,
        poll_seconds: float = 1.0,
        tenant_concurrency: int = DEFAULT_TENANT_CONCURRENCY,
    ) -> None:
        self.options = options
        self.worker_id = worker_id or f"framework-worker-{uuid.uuid4().hex}"
        self.lease_seconds = max(10, lease_seconds)
        self.heartbeat_seconds = max(1, min(heartbeat_seconds, self.lease_seconds // 2))
        self.poll_seconds = max(0.05, poll_seconds)
        self.tenant_concurrency = max(1, tenant_concurrency)

    async def claim_one(self) -> RunLease | None:
        async with create_db_session() as session:
            claimed = await claim_fair_run(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                tenant_concurrency=self.tenant_concurrency,
            )
            if claimed is None:
                return None
            task_id, run_id, version = claimed
            return RunLease(
                task_id=task_id,
                run_id=run_id,
                owner=self.worker_id,
                version=version,
            )

    async def renew_lease(self, lease: RunLease) -> bool:
        now = utc_now()
        async with create_db_session() as session:
            result = await session.exec(
                update(TaskRunEntity)
                .where(TaskRunEntity.id == lease.run_id)
                .where(TaskRunEntity.status == "running")
                .where(TaskRunEntity.lease_owner == lease.owner)
                .where(TaskRunEntity.lease_version == lease.version)
                .values(
                    lease_until=now + timedelta(seconds=self.lease_seconds),
                    last_heartbeat_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return result.rowcount == 1

    async def release_lease(self, lease: RunLease) -> bool:
        async with create_db_session() as session:
            initial_run = await session.get(TaskRunEntity, lease.run_id)
            if (
                initial_run is None
                or initial_run.lease_owner != lease.owner
                or initial_run.lease_version != lease.version
            ):
                return False
            task = await session.get(TaskEntity, initial_run.task_id)
            if task is None:
                result = await session.exec(
                    update(TaskRunEntity)
                    .where(TaskRunEntity.id == lease.run_id)
                    .where(TaskRunEntity.lease_owner == lease.owner)
                    .where(TaskRunEntity.lease_version == lease.version)
                    .values(lease_owner=None, lease_until=None)
                )
                await session.commit()
                return result.rowcount == 1

            quota = None
            if not initial_run.quota_slot_released:
                quota = await lock_quota_scope(
                    session,
                    service=task.service,
                    tenant_id=task.tenant_id,
                    resource_pool=initial_run.resource_pool,
                    max_concurrency=self.tenant_concurrency,
                    sync_limit=False,
                )
            run_result = await session.exec(
                select(TaskRunEntity)
                .where(TaskRunEntity.id == lease.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            run = run_result.first()
            if (
                run is None
                or run.lease_owner != lease.owner
                or run.lease_version != lease.version
            ):
                return False
            if not run.quota_slot_released:
                if quota is None:
                    raise RuntimeError(f"Run '{lease.run_id}' lost its quota scope while releasing its lease.")
                quota.running_count = max(0, quota.running_count - 1)
                quota.updated_at = utc_now()
                run.quota_slot_released = True
                session.add(quota)
            run.lease_owner = None
            run.lease_until = None
            run.updated_at = utc_now()
            session.add(run)
            await session.commit()
            return True

    async def run_once(self) -> bool:
        lease = await self.claim_one()
        if lease is None:
            return False
        reset_database_pool_metrics()
        service = TaskManagerService(self.options)
        execution_lease = ExecutionLease(
            run_id=lease.run_id,
            owner=lease.owner,
            version=lease.version,
        )
        executed = False
        with bind_execution_lease(execution_lease):
            heartbeat_stop: asyncio.Event | None = None
            heartbeat: asyncio.Task | None = None
            heartbeat_supervisor: LeaseHeartbeatSupervisor | None = None
            if os.environ.get("DB_TYPE", "sqlite") == "postgresql":
                heartbeat_supervisor = LeaseHeartbeatSupervisor(
                    lease=lease,
                    execution_lease=execution_lease,
                    lease_seconds=self.lease_seconds,
                    heartbeat_seconds=self.heartbeat_seconds,
                )
                heartbeat_supervisor.start()
            else:
                heartbeat_stop = asyncio.Event()
                heartbeat = asyncio.create_task(
                    self._heartbeat_loop(lease, heartbeat_stop, execution_lease),
                    name=f"task-manager-heartbeat:{lease.run_id}",
                )
            try:
                executed = await self._execute_run_with_trace(service, lease)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TaskManager worker failed for Run {}", lease.run_id)
                current = await service.get_task(lease.task_id)
                executed = current is not None and current.status in {"succeeded", "failed", "cancelled"}
            finally:
                if heartbeat_supervisor is not None:
                    await asyncio.to_thread(heartbeat_supervisor.stop)
                elif heartbeat_stop is not None and heartbeat is not None:
                    heartbeat_stop.set()
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                if not execution_lease.lost:
                    await self.release_lease(lease)
                pool_metrics = get_database_pool_metrics()
                if pool_metrics:
                    logger.info(
                        "TaskManager Run {} database pool metrics: {}",
                        lease.run_id,
                        pool_metrics,
                    )
        # A claimed but unexecuted run must back off, not immediately reclaim
        # itself thousands of times while reporting a fresh heartbeat.
        return executed

    async def _execute_run_with_trace(self, service: TaskManagerService, lease: RunLease) -> bool:
        tracer = trace.get_tracer("aituge.task-worker")
        with tracer.start_as_current_span("task.run") as span:
            span.set_attribute("task.id", lease.task_id)
            span.set_attribute("run.id", lease.run_id)
            return await service._drain_prepared_task(lease.task_id, run_id=lease.run_id)

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            try:
                claimed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TaskManager worker claim cycle failed")
                claimed = False
            if not claimed:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def _heartbeat_loop(
        self,
        lease: RunLease,
        stop_event: asyncio.Event,
        execution_lease: ExecutionLease,
    ) -> None:
        failures = 0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.heartbeat_seconds)
                return
            except asyncio.TimeoutError:
                pass
            try:
                if await self.renew_lease(lease):
                    failures = 0
                    continue
                failures += 1
            except Exception:
                failures += 1
                logger.exception("TaskManager Run {} heartbeat failed", lease.run_id)
            if failures >= 2:
                execution_lease.mark_lost("two consecutive lease renewal failures")
                logger.error("TaskManager Run {} lost its execution lease", lease.run_id)
                return


def build_worker_options() -> SchedulingRuntimeOptions:
    # Match the API's local-development default while production Compose
    # explicitly supplies /app/aituge-tmp as a shared volume.
    default_root = Path(__file__).resolve().parents[4] / "tmp"
    root = Path(os.environ.get("AITUGE_TMP_ROOT", default_root)).expanduser().resolve()
    return SchedulingRuntimeOptions(
        local_python_artifact_dir=root / "chat-artifacts",
        local_python_work_dir=root / "code-runs",
    )


async def main() -> None:
    configure_tracing_from_env(default_service_name="contract-review-framework-worker")
    await init_db()
    await assert_observability_schema_if_required()
    # Registries are process-local. A standalone Worker must load the same
    # capabilities, Agent profiles, and Skill packages as the API process
    # before it can execute a persisted Run.
    async with create_db_session() as session:
        await ensure_default_skill_packages(session)
        await ensure_default_agent_profiles(session)
        await mount_capabilities_from_env(session=session)
    worker = TaskWorker(
        build_worker_options(),
        worker_id=os.environ.get("TASK_WORKER_ID") or None,
        lease_seconds=int(os.environ.get("TASK_WORKER_LEASE_SECONDS", "120")),
        heartbeat_seconds=int(os.environ.get("TASK_WORKER_HEARTBEAT_SECONDS", "30")),
        poll_seconds=float(os.environ.get("TASK_WORKER_POLL_SECONDS", "1")),
        tenant_concurrency=int(os.environ.get("TASK_TENANT_CONCURRENCY", "10")),
    )
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
