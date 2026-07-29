from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from proof.errors import ProofError


TENANT_ID_HEADER = "X-Tenant-ID"
MAX_TENANT_ID_LENGTH = 64
INVALID_TENANT_ID_VALUES = frozenset({"null", "none", "undefined"})

_current_tenant_id: ContextVar[str | None] = ContextVar(
    "proof_current_tenant_id",
    default=None,
)


def normalize_tenant_id(value: str | None) -> str:
    tenant_id = str(value or "").strip()
    if not tenant_id or tenant_id.lower() in INVALID_TENANT_ID_VALUES:
        raise ProofError(
            "tenant_id_required",
            f"{TENANT_ID_HEADER} is required.",
            status_code=400,
        )
    if len(tenant_id) > MAX_TENANT_ID_LENGTH:
        raise ProofError(
            "invalid_tenant_id",
            f"{TENANT_ID_HEADER} must not exceed {MAX_TENANT_ID_LENGTH} characters.",
            status_code=400,
        )
    return tenant_id


def current_tenant_id() -> str:
    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        raise ProofError(
            "tenant_context_missing",
            "Proof tenant context is not available.",
            status_code=500,
        )
    return tenant_id


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    normalized = normalize_tenant_id(tenant_id)
    token: Token[str | None] = _current_tenant_id.set(normalized)
    try:
        yield normalized
    finally:
        _current_tenant_id.reset(token)


def tenant_storage_key(tenant_id: str | None = None) -> str:
    normalized = normalize_tenant_id(tenant_id) if tenant_id is not None else current_tenant_id()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
