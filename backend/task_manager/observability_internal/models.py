from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SecurityAuditEventEntity(SQLModel, table=True):
    __tablename__ = "tuge_security_audit_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_obs30_security_idempotency"),
        CheckConstraint(
            "scope_type IN ('TENANT','SYSTEM')",
            name="ck_obs30_security_scope",
        ),
        CheckConstraint(
            "(scope_type = 'TENANT' AND tenant_id IS NOT NULL) OR "
            "(scope_type = 'SYSTEM')",
            name="ck_obs30_security_tenant_scope",
        ),
        CheckConstraint(
            "audit_layer = 'PYTHON_EXECUTION'",
            name="ck_obs30_security_layer",
        ),
        CheckConstraint(
            "risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')",
            name="ck_obs30_security_risk",
        ),
        CheckConstraint(
            "outcome IN ('SUCCESS','FAILURE','DENIED','CANCELLED','TIMEOUT',"
            "'PARTIAL','UNKNOWN','ABANDONED')",
            name="ck_obs30_security_outcome",
        ),
        CheckConstraint(
            "actor_type IS NULL OR actor_type IN "
            "('USER','SERVICE','SYSTEM','ANONYMOUS','UNKNOWN')",
            name="ck_obs30_security_actor",
        ),
        Index(
            "idx_obs30_security_tenant_occurred",
            "tenant_id",
            "occurred_at",
            "id",
        ),
        Index(
            "idx_obs30_security_action",
            "audit_action_id",
            "occurred_at",
            "id",
        ),
    )

    id: str = Field(
        default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80
    )
    idempotency_key: str = Field(nullable=False, max_length=96)
    immutable_fingerprint: str = Field(nullable=False, max_length=64)
    audit_action_id: str = Field(nullable=False, max_length=96)
    access_session_id: str | None = Field(default=None, max_length=96)
    audit_layer: str = Field(default="PYTHON_EXECUTION", nullable=False, max_length=32)
    parent_audit_event_id: str | None = Field(default=None, max_length=96)
    scope_type: str = Field(nullable=False, max_length=16)
    schema_version: int = Field(default=1, ge=1)
    service: str = Field(nullable=False, max_length=80)
    tenant_id: str | None = Field(default=None, max_length=64)
    occurred_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime, nullable=False)
    )
    ingested_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime, nullable=False)
    )
    category: str = Field(nullable=False, max_length=80)
    action: str = Field(nullable=False, max_length=120)
    risk_level: str = Field(nullable=False, max_length=16)
    actor_type: str | None = Field(default=None, max_length=16)
    actor_id: str | None = Field(default=None, max_length=120)
    subject_type: str | None = Field(default=None, max_length=80)
    subject_id: str | None = Field(default=None, max_length=120)
    missing_context_reason: str | None = Field(default=None, sa_column=Column(Text))
    cross_tenant: bool = False
    reason_code: str | None = Field(default=None, max_length=80)
    source_ip_masked: str | None = Field(default=None, max_length=80)
    request_id: str | None = Field(default=None, max_length=120)
    trace_id: str | None = Field(default=None, max_length=64)
    outcome: str = Field(nullable=False, max_length=32)
    error_code: str | None = Field(default=None, max_length=120)
    display_code: str = Field(nullable=False, max_length=120)
    metadata_json: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )


class ScopeJtiClaimEntity(SQLModel, table=True):
    """Persistent one-time claim for a Scope JWT; raw subjects/JTIs are never stored."""

    __tablename__ = "tuge_obs30_scope_jti_claim"
    __table_args__ = (
        CheckConstraint(
            "expires_at > claimed_at",
            name="ck_obs30_scope_jti_claim_expiry",
        ),
        Index("idx_obs30_scope_jti_claim_expiry", "expires_at"),
        Index(
            "idx_obs30_scope_jti_claim_subject",
            "service_subject_hash",
            "claimed_at",
        ),
    )

    claim_hash: str = Field(primary_key=True, max_length=64)
    service_subject_hash: str = Field(nullable=False, max_length=64)
    expires_at: datetime = Field(sa_column=Column(DateTime, nullable=False))
    claimed_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime, nullable=False)
    )


class QuerySnapshotEntity(SQLModel, table=True):
    """Server-side state referenced by a short, non-descriptive handle."""

    __tablename__ = "tuge_obs30_query_snapshot"
    __table_args__ = (
        CheckConstraint("item_count >= 0", name="ck_obs30_snapshot_item_count"),
        CheckConstraint("payload_bytes >= 0", name="ck_obs30_snapshot_payload_bytes"),
        CheckConstraint("expires_at > created_at", name="ck_obs30_snapshot_expiry"),
        Index("idx_obs30_snapshot_expiry", "expires_at"),
        Index(
            "idx_obs30_snapshot_scope_created",
            "scope_hash",
            "created_at",
            "handle",
        ),
        Index(
            "idx_obs30_snapshot_resource",
            "resource_kind",
            "created_at",
            "handle",
        ),
    )

    handle: str = Field(primary_key=True, max_length=84)
    query_snapshot_id: str = Field(nullable=False, unique=True, max_length=80)
    resource_kind: str = Field(nullable=False, max_length=48)
    snapshot_mode: str = Field(nullable=False, max_length=40)
    scope_hash: str = Field(nullable=False, max_length=64)
    filter_hash: str = Field(nullable=False, max_length=64)
    snapshot_to: datetime = Field(sa_column=Column(DateTime, nullable=False))
    expires_at: datetime = Field(sa_column=Column(DateTime, nullable=False))
    max_ingested_at: datetime | None = Field(default=None, sa_column=Column(DateTime))
    max_sequence: int | None = None
    max_event_id: str | None = Field(default=None, max_length=80)
    item_count: int = Field(default=0, ge=0)
    payload_bytes: int = Field(default=0, ge=0)
    payload_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime, nullable=False)
    )


def new_query_snapshot_id() -> str:
    return "qs_" + uuid.uuid4().hex
