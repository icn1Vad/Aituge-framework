from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, Request

from .errors import TranslationError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


@dataclass(frozen=True, slots=True)
class InternalRequestContext:
    user_id: str
    tenant_id: str
    dept_id: str | None
    request_id: str
    idempotency_key: str | None


async def require_internal_context(
    request: Request,
    internal_token: Annotated[
        str | None, Header(alias="X-Internal-Token")
    ] = None,
    user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    dept_id: Annotated[str | None, Header(alias="X-Dept-Id")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> InternalRequestContext:
    expected = request.app.state.settings.internal_token.get_secret_value()
    if internal_token is None or not hmac.compare_digest(internal_token, expected):
        raise TranslationError(
            "UNAUTHORIZED_INTERNAL_CALL",
            "Valid internal service credentials are required",
            status_code=401,
        )

    normalized_user = _required_identifier("X-User-Id", user_id)
    normalized_tenant = _required_identifier("X-Tenant-Id", tenant_id)
    normalized_request = _required_identifier(
        "X-Request-Id", request_id or getattr(request.state, "request_id", None)
    )
    normalized_idempotency = idempotency_key.strip() if idempotency_key else None
    if normalized_idempotency is not None and not 8 <= len(normalized_idempotency) <= 160:
        raise TranslationError(
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must contain between 8 and 160 characters",
        )
    normalized_dept = dept_id.strip()[:128] if dept_id and dept_id.strip() else None
    return InternalRequestContext(
        user_id=normalized_user,
        tenant_id=normalized_tenant,
        dept_id=normalized_dept,
        request_id=normalized_request,
        idempotency_key=normalized_idempotency,
    )


def _required_identifier(name: str, value: str | None) -> str:
    normalized = (value or "").strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise TranslationError(
            "INVALID_INTERNAL_CONTEXT", f"{name} is missing or invalid"
        )
    return normalized


def require_idempotency_key(context: InternalRequestContext) -> str:
    if context.idempotency_key is None:
        raise TranslationError(
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key is required for translation creation",
        )
    return context.idempotency_key
