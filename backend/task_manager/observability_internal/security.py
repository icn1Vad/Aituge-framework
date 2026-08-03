from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from db.db_context import create_db_session
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .auth import InternalAuthContext
from .errors import InternalObservabilityError, audit_write_required
from .event_registry import (
    RegistryValidationError,
    contains_sensitive_material,
)
from .models import SecurityAuditEventEntity, utc_now
from .redaction import normalize_source_ip_masked, sanitized_security_metadata
from .schemas import Actor, InternalSecurityEvent, MetadataField, Subject

VALID_SCOPE_TYPES = {"TENANT", "SYSTEM"}
VALID_ACTOR_TYPES = {"USER", "SERVICE", "SYSTEM", "ANONYMOUS", "UNKNOWN"}
VALID_RISKS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
VALID_OUTCOMES = {
    "SUCCESS",
    "FAILURE",
    "DENIED",
    "CANCELLED",
    "TIMEOUT",
    "PARTIAL",
    "UNKNOWN",
    "ABANDONED",
}
ACCESS_RESULTS = {"SUCCESS", "DENIED", "NOT_FOUND", "FAILURE"}
TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SecuritySessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(slots=True)
class SecurityEventInput:
    audit_action_id: str
    service: str
    scope_type: str
    category: str
    action: str
    risk_level: str
    outcome: str
    display_code: str
    access_session_id: str | None = None
    parent_audit_event_id: str | None = None
    tenant_id: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    missing_context_reason: str | None = None
    cross_tenant: bool = False
    reason_code: str | None = None
    source_ip_masked: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    error_code: str | None = None
    schema_version: int = 1
    occurred_at: datetime | None = None
    metadata: list[dict[str, Any]] = field(default_factory=list)
    idempotency_key: str | None = None


class SecurityAuditRepository:
    """Keep append, query, and retention credentials independently injectable."""

    def __init__(
        self,
        *,
        append_session_factory: SecuritySessionFactory = create_db_session,
        query_session_factory: SecuritySessionFactory = create_db_session,
        retention_session_factory: SecuritySessionFactory = create_db_session,
    ) -> None:
        self._append_session_factory = append_session_factory
        self._query_session_factory = query_session_factory
        self._retention_session_factory = retention_session_factory

    async def append(
        self,
        row: SecurityAuditEventEntity,
    ) -> SecurityAuditEventEntity:
        async with self._append_session_factory() as session:
            bind = session.bind
            if bind is not None and bind.dialect.name == "postgresql":
                payload = json.dumps(
                    row.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                result = await session.execute(
                    text(
                        "SELECT * FROM "
                        "public.obs30_append_security_audit_event("
                        "CAST(:event_payload AS jsonb))"
                    ),
                    {"event_payload": payload},
                )
                persisted = dict(result.mappings().one())
                await session.commit()
                return SecurityAuditEventEntity.model_validate(persisted)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> SecurityAuditEventEntity | None:
        async with self._query_session_factory() as session:
            return (
                await session.exec(
                    select(SecurityAuditEventEntity).where(
                        SecurityAuditEventEntity.idempotency_key == idempotency_key
                    )
                )
            ).first()

    async def list_rows(self, statement: Any) -> list[SecurityAuditEventEntity]:
        async with self._query_session_factory() as session:
            return list((await session.exec(statement)).all())

    async def get(self, event_id: str) -> SecurityAuditEventEntity | None:
        async with self._query_session_factory() as session:
            return await session.get(SecurityAuditEventEntity, event_id)

    async def delete_before(self, cutoff: datetime, *, limit: int = 1000) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("security retention limit must be positive")
        async with self._retention_session_factory() as session:
            event_ids = list(
                (
                    await session.exec(
                        select(SecurityAuditEventEntity.id)
                        .where(SecurityAuditEventEntity.occurred_at < cutoff)
                        .order_by(
                            SecurityAuditEventEntity.occurred_at,
                            SecurityAuditEventEntity.id,
                        )
                        .limit(limit)
                    )
                ).all()
            )
            if not event_ids:
                return 0
            result = await session.execute(
                delete(SecurityAuditEventEntity).where(
                    SecurityAuditEventEntity.id.in_(tuple(event_ids))
                )
            )
            deleted = int(result.rowcount or 0)
            await session.commit()
            return deleted


class SecurityAuditService:
    def __init__(
        self,
        repository: SecurityAuditRepository | None = None,
    ) -> None:
        self.repository = repository or SecurityAuditRepository()

    async def append(self, event: SecurityEventInput) -> SecurityAuditEventEntity:
        normalized, metadata = self._validate(event)
        idempotency_key = normalized.idempotency_key or _idempotency_key(normalized)
        idempotency_key = _bounded_text(
            idempotency_key, field_name="idempotency_key", maximum=96
        )
        ingested_at = utc_now()
        occurred_at = normalized.occurred_at or ingested_at
        immutable_fingerprint = _immutable_fingerprint(
            normalized,
            metadata=metadata,
        )
        row = SecurityAuditEventEntity(
            idempotency_key=idempotency_key,
            immutable_fingerprint=immutable_fingerprint,
            audit_action_id=normalized.audit_action_id,
            access_session_id=normalized.access_session_id,
            audit_layer="PYTHON_EXECUTION",
            parent_audit_event_id=normalized.parent_audit_event_id,
            scope_type=normalized.scope_type,
            schema_version=normalized.schema_version,
            service=normalized.service,
            tenant_id=normalized.tenant_id,
            occurred_at=occurred_at,
            ingested_at=ingested_at,
            category=normalized.category,
            action=normalized.action,
            risk_level=normalized.risk_level,
            actor_type=normalized.actor_type,
            actor_id=normalized.actor_id,
            subject_type=normalized.subject_type,
            subject_id=normalized.subject_id,
            missing_context_reason=normalized.missing_context_reason,
            cross_tenant=normalized.cross_tenant,
            reason_code=normalized.reason_code,
            source_ip_masked=normalized.source_ip_masked,
            request_id=normalized.request_id,
            trace_id=normalized.trace_id,
            outcome=normalized.outcome,
            error_code=normalized.error_code,
            display_code=normalized.display_code,
            metadata_json=metadata,
        )
        try:
            return await self.repository.append(row)
        except IntegrityError:
            existing = await self.repository.find_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            if not _constant_time_text_equal(
                existing.immutable_fingerprint,
                immutable_fingerprint,
            ):
                raise InternalObservabilityError(
                    409,
                    "SECURITY_AUDIT_IDEMPOTENCY_CONFLICT",
                    normalized.request_id or "req_audit",
                ) from None
            return existing
        except DBAPIError as exc:
            if _postgres_error_code(exc) == "P3001":
                raise InternalObservabilityError(
                    409,
                    "SECURITY_AUDIT_IDEMPOTENCY_CONFLICT",
                    normalized.request_id or "req_audit",
                ) from None
            raise

    async def list_rows(self, statement: Any) -> list[SecurityAuditEventEntity]:
        return await self.repository.list_rows(statement)

    async def get(self, event_id: str) -> SecurityAuditEventEntity | None:
        return await self.repository.get(event_id)

    async def retention_delete_before(
        self,
        cutoff: datetime,
        *,
        limit: int = 1000,
    ) -> int:
        return await self.repository.delete_before(cutoff, limit=limit)

    async def record_internal_access(
        self,
        context: InternalAuthContext,
        *,
        action: str,
        subject_type: str,
        subject_id: str | None = None,
        sensitive: bool = False,
        decision_result: str,
        error_code: str | None = None,
    ) -> SecurityAuditEventEntity | None:
        if decision_result not in ACCESS_RESULTS:
            raise ValueError("unsupported access decision result")
        if not context.cross_tenant and not sensitive:
            return None
        if not context.audit_action_id:
            raise InternalObservabilityError(
                403,
                "INTERNAL_AUDIT_CONTEXT_REQUIRED",
                context.request_id,
            )
        if context.cross_tenant and (
            not context.parent_audit_event_id or not context.access_reason_code
        ):
            raise InternalObservabilityError(
                403,
                "INTERNAL_AUDIT_CONTEXT_REQUIRED",
                context.request_id,
            )

        tenant_id = (
            context.current_tenant_id if context.tenant_scope == "CURRENT" else None
        )
        scope_type = "TENANT" if tenant_id is not None else "SYSTEM"
        missing_parts: list[str] = []
        if tenant_id is None:
            missing_parts.append("MULTI_TENANT_SCOPE_HAS_NO_SINGLE_TENANT")
        if context.actor_id is None:
            missing_parts.append("ACTOR_ID_UNAVAILABLE")
        if subject_id is None:
            missing_parts.append("SUBJECT_ID_NOT_APPLICABLE")
        outcome = {
            "SUCCESS": "SUCCESS",
            "DENIED": "DENIED",
            "NOT_FOUND": "FAILURE",
            "FAILURE": "FAILURE",
        }[decision_result]
        reason_code = (
            "RESOURCE_NOT_FOUND"
            if decision_result == "NOT_FOUND"
            else context.access_reason_code
        )
        event = SecurityEventInput(
            audit_action_id=context.audit_action_id,
            access_session_id=context.access_session_id,
            parent_audit_event_id=context.parent_audit_event_id,
            service="python-observability",
            scope_type=scope_type,
            tenant_id=tenant_id,
            category="OBSERVABILITY_ACCESS",
            action=action,
            risk_level="HIGH" if context.cross_tenant else "MEDIUM",
            actor_type=context.actor_type,
            actor_id=context.actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            missing_context_reason=";".join(missing_parts) or None,
            cross_tenant=context.cross_tenant,
            reason_code=reason_code,
            request_id=None if context.access_session_id else context.request_id,
            outcome=outcome,
            error_code=error_code,
            display_code=action,
            metadata=[
                {
                    "key": "decision_result",
                    "value": decision_result,
                    "masked": False,
                },
                {
                    "key": "resource_type",
                    "value": subject_type,
                    "masked": False,
                },
                {
                    "key": "scope_kind",
                    "value": context.tenant_scope,
                    "masked": False,
                },
            ],
            idempotency_key=_access_idempotency_key(
                context,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                decision_result=decision_result,
            ),
        )
        try:
            return await self.append(event)
        except (DBAPIError, IntegrityError, RegistryValidationError, ValueError):
            raise audit_write_required(context.request_id) from None
        except InternalObservabilityError as exc:
            if exc.code == "SECURITY_AUDIT_IDEMPOTENCY_CONFLICT":
                raise audit_write_required(context.request_id) from None
            raise

    @staticmethod
    def to_dto(row: SecurityAuditEventEntity) -> InternalSecurityEvent:
        return InternalSecurityEvent(
            event_id=row.id,
            audit_action_id=row.audit_action_id,
            access_session_id=row.access_session_id,
            audit_layer=row.audit_layer,
            parent_audit_event_id=row.parent_audit_event_id,
            scope_type=row.scope_type,
            schema_version=row.schema_version,
            service=row.service,
            tenant_id=row.tenant_id,
            occurred_at=row.occurred_at,
            ingested_at=row.ingested_at,
            category=row.category,
            action=row.action,
            risk_level=row.risk_level,
            actor=(
                Actor(type=row.actor_type, id=row.actor_id) if row.actor_type else None
            ),
            subject=(
                Subject(type=row.subject_type, id=row.subject_id)
                if row.subject_type
                else None
            ),
            missing_context_reason=row.missing_context_reason,
            cross_tenant=row.cross_tenant,
            reason_code=row.reason_code,
            source_ip_masked=row.source_ip_masked,
            request_id=row.request_id,
            trace_id=row.trace_id,
            outcome=row.outcome,
            error_code=row.error_code,
            display_code=row.display_code,
            metadata=[
                MetadataField.model_validate(item) for item in (row.metadata_json or [])
            ],
        )

    @staticmethod
    def _validate(
        event: SecurityEventInput,
    ) -> tuple[SecurityEventInput, list[dict[str, Any]]]:
        if event.scope_type not in VALID_SCOPE_TYPES:
            raise ValueError("unsupported scope_type")
        if event.scope_type == "TENANT" and not event.tenant_id:
            raise ValueError("TENANT security events require tenant_id")
        if event.actor_type and event.actor_type not in VALID_ACTOR_TYPES:
            raise ValueError("unsupported actor_type")
        if event.risk_level not in VALID_RISKS:
            raise ValueError("unsupported risk_level")
        if event.outcome not in VALID_OUTCOMES:
            raise ValueError("unsupported outcome")
        if (
            isinstance(event.schema_version, bool)
            or not isinstance(event.schema_version, int)
            or event.schema_version < 1
        ):
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(event.cross_tenant, bool):
            raise TypeError("cross_tenant must be boolean")

        normalized = replace(
            event,
            audit_action_id=_bounded_text(
                event.audit_action_id, field_name="audit_action_id", maximum=96
            ),
            access_session_id=_optional_text(
                event.access_session_id, field_name="access_session_id", maximum=96
            ),
            parent_audit_event_id=_optional_text(
                event.parent_audit_event_id,
                field_name="parent_audit_event_id",
                maximum=96,
            ),
            service=_bounded_text(event.service, field_name="service", maximum=80),
            tenant_id=_optional_text(
                event.tenant_id, field_name="tenant_id", maximum=64
            ),
            category=_bounded_text(event.category, field_name="category", maximum=80),
            action=_bounded_text(event.action, field_name="action", maximum=120),
            actor_id=_optional_text(event.actor_id, field_name="actor_id", maximum=120),
            subject_type=_optional_text(
                event.subject_type, field_name="subject_type", maximum=80
            ),
            subject_id=_optional_text(
                event.subject_id, field_name="subject_id", maximum=120
            ),
            missing_context_reason=_optional_text(
                event.missing_context_reason,
                field_name="missing_context_reason",
                maximum=512,
            ),
            reason_code=_optional_text(
                event.reason_code, field_name="reason_code", maximum=80
            ),
            source_ip_masked=normalize_source_ip_masked(event.source_ip_masked),
            request_id=_optional_text(
                event.request_id, field_name="request_id", maximum=120
            ),
            trace_id=_optional_trace_id(event.trace_id),
            error_code=_optional_text(
                event.error_code, field_name="error_code", maximum=120
            ),
            display_code=_bounded_text(
                event.display_code, field_name="display_code", maximum=120
            ),
            occurred_at=_normalize_occurred_at(event.occurred_at),
            idempotency_key=_optional_text(
                event.idempotency_key, field_name="idempotency_key", maximum=96
            ),
        )
        missing_context = (
            normalized.tenant_id is None
            or normalized.actor_type is None
            or normalized.actor_id is None
            or normalized.subject_type is None
            or normalized.subject_id is None
        )
        if missing_context and not normalized.missing_context_reason:
            raise ValueError(
                "missing_context_reason is required when audit context is incomplete"
            )
        metadata = sanitized_security_metadata(
            normalized.metadata,
            require_decision_result=True,
        )
        return normalized, metadata


def _immutable_fingerprint(
    event: SecurityEventInput,
    *,
    metadata: list[dict[str, Any]],
) -> str:
    facts = {
        "auditActionId": event.audit_action_id,
        "accessSessionId": event.access_session_id,
        "auditLayer": "PYTHON_EXECUTION",
        "parentAuditEventId": event.parent_audit_event_id,
        "scopeType": event.scope_type,
        "schemaVersion": event.schema_version,
        "service": event.service,
        "tenantId": event.tenant_id,
        "occurredAt": event.occurred_at.isoformat() if event.occurred_at else None,
        "category": event.category,
        "action": event.action,
        "riskLevel": event.risk_level,
        "actorType": event.actor_type,
        "actorId": event.actor_id,
        "subjectType": event.subject_type,
        "subjectId": event.subject_id,
        "missingContextReason": event.missing_context_reason,
        "crossTenant": event.cross_tenant,
        "reasonCode": event.reason_code,
        "sourceIpMasked": event.source_ip_masked,
        "requestId": event.request_id,
        "traceId": event.trace_id,
        "outcome": event.outcome,
        "errorCode": event.error_code,
        "displayCode": event.display_code,
        "metadata": metadata,
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key(event: SecurityEventInput) -> str:
    raw = "|".join(
        (
            event.audit_action_id,
            event.service,
            event.action,
            event.access_session_id or "",
            event.parent_audit_event_id or "",
            event.subject_type or "",
            event.subject_id or "",
            event.request_id or "",
        )
    )
    return "sec_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _access_idempotency_key(
    context: InternalAuthContext,
    *,
    action: str,
    subject_type: str,
    subject_id: str | None,
    decision_result: str,
) -> str:
    raw = "|".join(
        (
            context.audit_action_id or "",
            context.access_session_id or "",
            context.request_id if context.access_session_id is None else "",
            action,
            subject_type,
            subject_id or "",
            decision_result,
        )
    )
    return "sec_access_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bounded_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        or contains_sensitive_material(normalized)
    ):
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _optional_text(
    value: object,
    *,
    field_name: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name, maximum=maximum)


def _optional_trace_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not TRACE_ID_PATTERN.fullmatch(value):
        raise ValueError("trace_id is invalid")
    return value


def _normalize_occurred_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("occurred_at must be a datetime")
    normalized = (
        value.astimezone(UTC).replace(tzinfo=None)
        if value.tzinfo is not None
        else value
    )
    if normalized > utc_now() + timedelta(seconds=5):
        raise ValueError("occurred_at cannot be in the future")
    return normalized


def _constant_time_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _postgres_error_code(exc: DBAPIError) -> str | None:
    original = exc.orig
    return (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or getattr(getattr(original, "__cause__", None), "sqlstate", None)
    )


def hmac_compare(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)
