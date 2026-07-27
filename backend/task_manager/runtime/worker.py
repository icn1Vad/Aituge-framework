from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import update
from sqlmodel import select

from db.db_context import create_db_session, init_db
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskEntity, TaskRunEntity, utc_now
from task_manager.runtime.fencing import ExecutionLease, bind_execution_lease
from task_manager.runtime.quota import DEFAULT_TENANT_CONCURRENCY, claim_fair_run
from task_manager.service import TaskManagerService


@dataclass(frozen=True, slots=True)
class RunLease:
    task_id: str
    run_id: str
    owner: str
    version: int


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
            result = await session.exec(
                update(TaskRunEntity)
                .where(TaskRunEntity.id == lease.run_id)
                .where(TaskRunEntity.lease_owner == lease.owner)
                .where(TaskRunEntity.lease_version == lease.version)
                .values(lease_owner=None, lease_until=None)
            )
            await session.commit()
            return result.rowcount == 1

    async def run_once(self) -> bool:
        lease = await self.claim_one()
        if lease is None:
            return False
        service = TaskManagerService(self.options)
        execution_lease = ExecutionLease(
            run_id=lease.run_id,
            owner=lease.owner,
            version=lease.version,
        )
        with bind_execution_lease(execution_lease):
            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(lease, heartbeat_stop, execution_lease),
                name=f"task-manager-heartbeat:{lease.run_id}",
            )
            try:
                await service._drain_prepared_task(
                    lease.task_id,
                    run_id=lease.run_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TaskManager worker failed for Run {}", lease.run_id)
            finally:
                heartbeat_stop.set()
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                if not execution_lease.lost:
                    await self.release_lease(lease)
        return True

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
    root = Path(os.environ.get("AITUGE_TMP_ROOT", "/app/aituge-tmp")).expanduser()
    return SchedulingRuntimeOptions(
        local_python_artifact_dir=root / "chat-artifacts",
        local_python_work_dir=root / "code-runs",
    )


async def main() -> None:
    await init_db()
    worker = TaskWorker(
        build_worker_options(),
        worker_id=os.environ.get("TASK_WORKER_ID") or None,
        lease_seconds=int(os.environ.get("TASK_WORKER_LEASE_SECONDS", "120")),
        heartbeat_seconds=int(os.environ.get("TASK_WORKER_HEARTBEAT_SECONDS", "30")),
        poll_seconds=float(os.environ.get("TASK_WORKER_POLL_SECONDS", "1")),
        tenant_concurrency=int(os.environ.get("TASK_TENANT_CONCURRENCY", "2")),
    )
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
