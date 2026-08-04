"""Page-8 wiring for the internal observability routers and maintenance jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db.db_context import create_db_session, get_engine
from fastapi import APIRouter, FastAPI, HTTPException, Request
from model_observability.api import create_model_observability_router
from model_observability.query import ModelInvocationQueryService, ModelQueryScope
from model_observability.snapshot import ModelSnapshotStore
from task_manager.observability_internal.auth import (
    HmacInternalAuthenticator,
    ScopeJtiClaimRepository,
)
from task_manager.observability_internal.capabilities import CapabilitySigner
from task_manager.observability_internal.errors import InternalObservabilityError
from task_manager.observability_internal.router import (
    create_task_security_observability_router,
)
from task_manager.observability_internal.security import SecurityAuditService
from task_manager.observability_internal.snapshots import SnapshotStore

from backend.observability_migrate import assert_observability_ready

_LOGGER = logging.getLogger(__name__)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SAFE_CONFIG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_VIEW_PERMISSION = "monitor:observability:view"
_DETAIL_PERMISSION = "monitor:observability:detail"


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def _required_config(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or _SAFE_CONFIG.fullmatch(value) is None:
        raise RuntimeError(f"{name} is missing or invalid")
    return value


def _bounded_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} is invalid") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} is outside the allowed range")
    return value


def _read_secret_file(name: str) -> bytes:
    raw_path = os.getenv(name, "").strip()
    if not raw_path:
        raise RuntimeError(f"{name} is required")
    path = Path(raw_path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{name} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_size < 32
        or metadata.st_size > 4096
    ):
        raise RuntimeError(f"{name} has unsafe metadata")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_nlink != 1
        ):
            raise RuntimeError(f"{name} changed during validation")
        value = os.read(descriptor, 4097).strip()
    finally:
        os.close(descriptor)
    if len(value) < 32 or len(value) > 4096:
        raise RuntimeError(f"{name} has an invalid length")
    return value


def trusted_asgi_mtls_identity(request: Request) -> str | None:
    """Read identity only from server-owned ASGI scope, never an HTTP header."""

    value = request.scope.get("mtls_client_subject")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized != value or _SAFE_CONFIG.fullmatch(normalized) is None:
        return None
    return normalized


class SharedModelObservabilityAuthorizer:
    """Reuse Page-3 authentication and audit for Page-2 model queries."""

    def __init__(
        self,
        authenticator: HmacInternalAuthenticator,
        security: SecurityAuditService,
    ) -> None:
        self._authenticator = authenticator
        self._security = security

    async def authorize(
        self,
        *,
        request: Request,
        scope_token: str,
        request_id: str,
    ) -> ModelQueryScope:
        del scope_token, request_id
        try:
            context = await self._authenticator.authenticate(request)
            detail = _is_model_detail(request.url.path)
            required = _DETAIL_PERMISSION if detail else _VIEW_PERMISSION
            if required not in context.permissions:
                raise InternalObservabilityError(
                    403,
                    "INTERNAL_SCOPE_FORBIDDEN",
                    context.request_id,
                )
            await self._security.record_internal_access(
                context,
                action=(
                    "OBSERVABILITY_MODEL_DETAIL"
                    if detail
                    else "OBSERVABILITY_MODEL_QUERY"
                ),
                subject_type="MODEL_INVOCATION",
                subject_id=_model_subject_id(request.url.path) if detail else None,
                sensitive=detail,
                decision_result="SUCCESS",
            )
        except InternalObservabilityError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "retryable": exc.status_code in {429, 503}},
            ) from None

        if context.tenant_scope == "ALL":
            tenant_ids: tuple[str, ...] = ()
            all_tenants = True
        else:
            tenant_ids = tuple(sorted(context.tenant_ids))
            all_tenants = False
        return ModelQueryScope(
            tenant_ids=tenant_ids,
            scope_key=context.scope_fingerprint,
            environment=context.environment,
            permission_claims=tuple(sorted(context.permissions)),
            scope_claims=(
                context.tenant_scope,
                context.audit_action_id or "",
                context.access_session_id or "",
            ),
            all_tenants=all_tenants,
        )


def _is_model_detail(path: str) -> bool:
    prefix = "/internal/observability/v1/model-invocations/"
    return path.startswith(prefix) and len(path) > len(prefix)


def _model_subject_id(path: str) -> str | None:
    value = path.rsplit("/", 1)[-1]
    return value if value and _SAFE_CONFIG.fullmatch(value) is not None else None


@dataclass(slots=True)
class ObservabilityRuntime:
    routers: tuple[APIRouter, ...]
    schema_required: bool
    task_snapshots: SnapshotStore | None = None
    model_snapshots: ModelSnapshotStore | None = None
    scope_claims: ScopeJtiClaimRepository | None = None
    security: SecurityAuditService | None = None
    _maintenance_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.schema_required:
            await assert_observability_ready(get_engine())
        if not self.routers:
            return
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name="observability-maintenance",
        )

    async def stop(self) -> None:
        task = self._maintenance_task
        self._maintenance_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _maintenance_loop(self) -> None:
        interval = _bounded_int_env(
            "OBSERVABILITY_MAINTENANCE_INTERVAL_SECONDS",
            300,
            minimum=60,
            maximum=3600,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                if self.scope_claims is not None:
                    await self.scope_claims.cleanup_expired(limit=1000)
                if self.task_snapshots is not None:
                    await self.task_snapshots.cleanup_expired()
                if self.model_snapshots is not None:
                    await self.model_snapshots.cleanup_expired(limit=1000)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - maintenance must not stop business traffic
                _LOGGER.warning(
                    "observability_maintenance_failed code="
                    "OBSERVABILITY_MAINTENANCE_FAILED"
                )


def create_observability_runtime_from_env() -> ObservabilityRuntime:
    internal_enabled = _enabled("OBSERVABILITY_INTERNAL_ENABLED")
    schema_required = internal_enabled or _enabled("MODEL_INVOCATION_LEDGER_ENABLED")
    if not internal_enabled:
        return ObservabilityRuntime(routers=(), schema_required=schema_required)

    environment = _required_config("OBSERVABILITY_ENVIRONMENT")
    if environment not in {"test", "formal"}:
        raise RuntimeError("OBSERVABILITY_ENVIRONMENT must be test or formal")
    service_secret = _read_secret_file("OBSERVABILITY_INTERNAL_SERVICE_JWT_SECRET_FILE")
    scope_secret = _read_secret_file("OBSERVABILITY_INTERNAL_SCOPE_JWT_SECRET_FILE")
    capability_secret = _read_secret_file(
        "OBSERVABILITY_INTERNAL_CAPABILITY_SECRET_FILE"
    )
    scope_claims = ScopeJtiClaimRepository()
    authenticator = HmacInternalAuthenticator(
        service_secret=service_secret,
        scope_secret=scope_secret,
        service_issuer=_required_config("OBSERVABILITY_INTERNAL_SERVICE_JWT_ISSUER"),
        service_audience=_required_config(
            "OBSERVABILITY_INTERNAL_SERVICE_JWT_AUDIENCE"
        ),
        scope_issuer=_required_config("OBSERVABILITY_INTERNAL_SCOPE_JWT_ISSUER"),
        scope_audience=_required_config("OBSERVABILITY_INTERNAL_SCOPE_JWT_AUDIENCE"),
        environment=environment,
        client_identity_verifier=trusted_asgi_mtls_identity,
        scope_claim_repository=scope_claims,
    )
    security = SecurityAuditService()
    task_snapshots = SnapshotStore(CapabilitySigner(capability_secret))
    model_snapshots = ModelSnapshotStore(create_db_session)
    model_query = ModelInvocationQueryService(
        create_db_session,
        snapshot_store=model_snapshots,
    )
    model_authorizer = SharedModelObservabilityAuthorizer(
        authenticator,
        security,
    )
    return ObservabilityRuntime(
        routers=(
            create_task_security_observability_router(
                authenticator=authenticator,
                snapshots=task_snapshots,
                security=security,
            ),
            create_model_observability_router(
                query_service=model_query,
                authorizer=model_authorizer,
            ),
        ),
        schema_required=True,
        task_snapshots=task_snapshots,
        model_snapshots=model_snapshots,
        scope_claims=scope_claims,
        security=security,
    )


def install_observability_openapi_contract(app: FastAPI) -> None:
    """Remove framework-generated 422 responses from the frozen internal API."""

    original_openapi = app.openapi

    def observability_openapi() -> dict[str, Any]:
        schema = original_openapi()
        for path, operations in schema.get("paths", {}).items():
            if not path.startswith("/internal/observability/v1/"):
                continue
            for method, operation in operations.items():
                if method.lower() not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "options",
                    "head",
                    "trace",
                }:
                    continue
                operation.get("responses", {}).pop("422", None)
        return schema

    app.openapi = observability_openapi


async def assert_observability_schema_if_required() -> None:
    if _enabled("MODEL_INVOCATION_LEDGER_ENABLED") or _enabled(
        "OBSERVABILITY_INTERNAL_ENABLED"
    ):
        await assert_observability_ready(get_engine())
