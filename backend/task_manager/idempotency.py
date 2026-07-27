from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import TaskCreateRequest, TaskRunRequest


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused with a different request."""

    code = "IDEMPOTENCY_CONFLICT"


def canonical_json(value: Any) -> str:
    """Serialize JSON with one stable representation for hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_task_request_fingerprint(
    *,
    service_name: str,
    request: TaskCreateRequest,
) -> str:
    return sha256_fingerprint(
        {
            "schema": "framework-task-request-v1",
            "service": service_name,
            "tenant_id": request.tenant_id,
            "user_id": request.user_id,
            "request": request.model_dump(mode="json"),
        }
    )


def build_run_request_fingerprint(
    *,
    service_name: str,
    task_id: str,
    tenant_id: str,
    user_id: str,
    request: TaskRunRequest,
) -> str:
    return sha256_fingerprint(
        {
            "schema": "framework-run-request-v1",
            "service": service_name,
            "task_id": task_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "request": request.model_dump(mode="json"),
        }
    )


def assert_fingerprint_matches(
    *,
    stored_fingerprint: str,
    request_fingerprint: str,
    resource: str,
    idempotency_key: str,
) -> None:
    if stored_fingerprint == request_fingerprint:
        return
    raise IdempotencyConflictError(
        f"Idempotency key '{idempotency_key}' for {resource} was already used "
        "with a different request."
    )
