from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import threading
from typing import Any

import psycopg

from contract.config import Settings
from contract.errors import ContractError


_GATE_LOCK = threading.Lock()
_GATES: dict[tuple[str, int, float, str], threading.BoundedSemaphore] = {}


def _gate_for(settings: Settings) -> threading.BoundedSemaphore:
    key = (
        settings.database_url,
        settings.database_max_connections,
        settings.database_connection_acquire_timeout_seconds,
        settings.database_application_name,
    )
    with _GATE_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = threading.BoundedSemaphore(settings.database_max_connections)
            _GATES[key] = gate
        return gate


@contextmanager
def open_contract_database_connection(
    settings: Settings,
    *,
    row_factory: Any | None = None,
) -> Iterator[Any]:
    """Open one short-lived Contract connection within the process-wide limit."""
    gate = _gate_for(settings)
    acquired = gate.acquire(
        timeout=settings.database_connection_acquire_timeout_seconds,
    )
    if not acquired:
        raise ContractError(
            "CONTRACT_DATABASE_CAPACITY_EXCEEDED",
            "Contract database connection capacity is busy.",
            status_code=503,
            retryable=True,
            details={
                "max_connections": settings.database_max_connections,
            },
        )

    try:
        connect_kwargs: dict[str, Any] = {
            "connect_timeout": 5,
            "application_name": settings.database_application_name,
        }
        if row_factory is not None:
            connect_kwargs["row_factory"] = row_factory
        with psycopg.connect(settings.database_url, **connect_kwargs) as connection:
            yield connection
    finally:
        gate.release()


def reset_contract_connection_gates_for_test() -> None:
    """Clear process-local gates after a test changes its database settings."""
    with _GATE_LOCK:
        _GATES.clear()
