from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from db.db_context import create_db_session
from sqlmodel import select

from task_manager.models import TaskRunEntity


class RunLeaseLost(RuntimeError):
    """Raised when a worker no longer owns the fenced Run lease."""


@dataclass
class ExecutionLease:
    run_id: str
    owner: str
    version: int
    lost: bool = False
    loss_reason: str = ""

    def mark_lost(self, reason: str = "execution lease was lost") -> None:
        self.lost = True
        self.loss_reason = reason

    def assert_active(self) -> None:
        if self.lost:
            detail = f": {self.loss_reason}" if self.loss_reason else ""
            raise RunLeaseLost(f"Run '{self.run_id}' no longer owns its execution lease{detail}.")


_CURRENT_EXECUTION_LEASE: ContextVar[ExecutionLease | None] = ContextVar(
    "task_manager_execution_lease",
    default=None,
)


@contextmanager
def bind_execution_lease(lease: ExecutionLease) -> Iterator[ExecutionLease]:
    token = _CURRENT_EXECUTION_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_EXECUTION_LEASE.reset(token)


def current_execution_lease() -> ExecutionLease | None:
    return _CURRENT_EXECUTION_LEASE.get()


def assert_execution_lease_scope(run_id: str) -> ExecutionLease | None:
    lease = current_execution_lease()
    if lease is None:
        return None
    lease.assert_active()
    if lease.run_id != run_id:
        raise RunLeaseLost(
            f"Execution lease for Run '{lease.run_id}' cannot write Run '{run_id}'."
        )
    return lease


async def verify_execution_lease(
    session: Any,
    run_id: str,
    *,
    run: TaskRunEntity | None = None,
) -> TaskRunEntity | None:
    """Lock and verify the current worker lease when execution context is active."""

    lease = assert_execution_lease_scope(run_id)
    if lease is None:
        return None
    if run is None:
        statement = (
            select(TaskRunEntity)
            .where(TaskRunEntity.id == run_id)
            .with_for_update()
        )
        run = (await session.exec(statement)).first()
    if (
        run is None
        or run.status != "running"
        or run.lease_owner != lease.owner
        or run.lease_version != lease.version
    ):
        raise RunLeaseLost(
            f"Run '{run_id}' is no longer owned by worker '{lease.owner}' "
            f"at lease version {lease.version}."
        )
    lease.assert_active()
    return run


async def verify_current_execution_lease(run_id: str) -> TaskRunEntity | None:
    """Recheck the bound execution lease immediately before an external side effect."""

    lease = assert_execution_lease_scope(run_id)
    if lease is None:
        return None
    async with create_db_session() as session:
        run = await verify_execution_lease(session, run_id)
    lease.assert_active()
    return run
