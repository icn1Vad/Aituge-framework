from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .domain import utc_now


MODEL_OBSERVABILITY_METADATA = MetaData()


class ModelObservabilityBase(DeclarativeBase):
    metadata = MODEL_OBSERVABILITY_METADATA


class ModelObservabilitySequenceEntity(ModelObservabilityBase):
    """Database-owned monotonic counters used for causal event ordering."""

    __tablename__ = "tuge_model_observability_sequence"
    __table_args__ = (
        CheckConstraint(
            "sequence_value >= 0",
            name="ck_tuge_model_observability_sequence_value",
        ),
    )

    sequence_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    sequence_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

class ModelQuerySnapshotEntity(ModelObservabilityBase):
    """Shared, bounded materialized query result used by every Python worker."""

    __tablename__ = "tuge_model_observability_query_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "query_snapshot_id",
            name="uq_tuge_model_query_snapshot_id",
        ),
        CheckConstraint("row_count >= 0", name="ck_tuge_model_snapshot_row_count"),
        CheckConstraint(
            "storage_bytes >= 0",
            name="ck_tuge_model_snapshot_storage_bytes",
        ),
        CheckConstraint(
            "snapshot_mode IN ('MATERIALIZED_RESULT_SET',"
            "'APPEND_ONLY_HIGH_WATERMARK')",
            name="ck_tuge_model_snapshot_mode",
        ),
        CheckConstraint(
            "json_type(rows_json) = 'array' "
            "AND json_array_length(rows_json) = row_count",
            name="ck_tuge_model_snapshot_rows_match",
        ),
        CheckConstraint(
            "max_sequence IS NULL OR max_sequence >= 0",
            name="ck_tuge_model_snapshot_max_sequence",
        ),
        Index(
            "idx_tuge_model_snapshot_reuse",
            "scope_hash",
            "query_hash",
            "created_at",
        ),
        Index("idx_tuge_model_snapshot_expiry", "expires_at"),
    )

    high_watermark_handle: Mapped[str] = mapped_column(String(84), primary_key=True)
    query_snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_signing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    max_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_event_id: Mapped[str | None] = mapped_column(String(80))
    max_sequence: Mapped[int | None] = mapped_column(BigInteger)
    data_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



_EVENT_TYPES_SQL = (
    "'MODEL_INVOCATION_STARTED',"
    "'MODEL_INVOCATION_DISPATCHED',"
    "'MODEL_INVOCATION_DISPATCH_UNKNOWN',"
    "'MODEL_INVOCATION_SUCCEEDED',"
    "'MODEL_INVOCATION_FAILED',"
    "'MODEL_INVOCATION_VALIDATION_FAILED',"
    "'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',"
    "'MODEL_INVOCATION_OUTCOME_UNKNOWN',"
    "'MODEL_INVOCATION_ABANDONED'"
)
_TERMINAL_TYPES_SQL = (
    "'MODEL_INVOCATION_SUCCEEDED',"
    "'MODEL_INVOCATION_FAILED',"
    "'MODEL_INVOCATION_VALIDATION_FAILED',"
    "'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',"
    "'MODEL_INVOCATION_OUTCOME_UNKNOWN',"
    "'MODEL_INVOCATION_ABANDONED'"
)
_OUTCOMES_SQL = (
    "'SUCCESS','FAILURE','DENIED','CANCELLED','TIMEOUT','PARTIAL','UNKNOWN','ABANDONED'"
)

_PROVIDER_HASH_SQL = (
    "provider_request_id_hash IS NULL OR "
    "(length(provider_request_id_hash) = 64 "
    "AND provider_request_id_hash = lower(provider_request_id_hash) "
    "AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(provider_request_id_hash, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)"
)
_CURRENCY_SQL = (
    "cost_currency IS NULL OR "
    "(length(cost_currency) = 3 "
    "AND substr(cost_currency, 1, 1) BETWEEN 'A' AND 'Z' "
    "AND substr(cost_currency, 2, 1) BETWEEN 'A' AND 'Z' "
    "AND substr(cost_currency, 3, 1) BETWEEN 'A' AND 'Z')"
)


class ModelInvocationEventEntity(ModelObservabilityBase):
    __tablename__ = "tuge_model_invocation_event"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_EVENT_TYPES_SQL})",
            name="ck_tuge_model_event_type",
        ),
        CheckConstraint("schema_version = 1", name="ck_tuge_model_event_schema"),
        CheckConstraint(
            "dispatch_status IN ('NOT_DISPATCHED','DISPATCHED','DISPATCH_UNKNOWN')",
            name="ck_tuge_model_event_dispatch_status",
        ),
        CheckConstraint(
            "privacy_mode IN ('STANDARD','PRIVATE')",
            name="ck_tuge_model_event_privacy_mode",
        ),
        CheckConstraint(
            "route_type IN ('LOCAL','EXTERNAL')",
            name="ck_tuge_model_event_route_type",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_tuge_model_event_attempt"),
        CheckConstraint("server_sequence >= 1", name="ck_tuge_model_event_server_sequence"),
        CheckConstraint(
            "fallback_from_invocation_id IS NULL OR "
            "fallback_from_invocation_id <> invocation_id",
            name="ck_tuge_model_event_fallback",
        ),
        CheckConstraint(
            "input_token_count IS NULL OR input_token_count >= 0",
            name="ck_tuge_model_event_input_tokens",
        ),
        CheckConstraint(
            "output_token_count IS NULL OR output_token_count >= 0",
            name="ck_tuge_model_event_output_tokens",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_tuge_model_event_latency",
        ),
        CheckConstraint(
            "time_to_first_token_ms IS NULL OR time_to_first_token_ms >= 0",
            name="ck_tuge_model_event_ttft",
        ),
        CheckConstraint(
            "cost_amount IS NULL OR cost_amount >= 0",
            name="ck_tuge_model_event_cost",
        ),
        CheckConstraint(
            _CURRENCY_SQL,
            name="ck_tuge_model_event_currency",
        ),
        CheckConstraint(
            "cost_source IS NULL OR cost_source IN ('PROVIDER','ESTIMATED')",
            name="ck_tuge_model_event_cost_source",
        ),
        CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL "
            "AND cost_source IS NULL AND pricing_version IS NULL "
            "AND cost_calculated_at IS NULL) "
            "OR (cost_amount IS NOT NULL AND cost_currency IS NOT NULL "
            "AND cost_source IS NOT NULL)",
            name="ck_tuge_model_event_cost_metadata",
        ),
        CheckConstraint(
            _PROVIDER_HASH_SQL,
            name="ck_tuge_model_event_provider_request_hash",
        ),
        CheckConstraint(
            "json_type(metadata_json) = 'object'",
            name="ck_tuge_model_event_metadata_object",
        ),
        CheckConstraint(
            "retry_reason IS NULL OR retry_reason IN "
            "('PROVIDER_RETRY','OUTPUT_REPAIR','GUARDRAIL_RETRY')",
            name="ck_tuge_model_event_retry_reason",
        ),
        CheckConstraint(
            "time_to_first_token_ms IS NULL OR latency_ms IS NULL "
            "OR time_to_first_token_ms <= latency_ms",
            name="ck_tuge_model_event_ttft_latency",
        ),
        CheckConstraint(
            f"event_type IN ({_TERMINAL_TYPES_SQL}) OR "
            "(input_token_count IS NULL AND output_token_count IS NULL "
            "AND latency_ms IS NULL AND time_to_first_token_ms IS NULL "
            "AND outcome IS NULL AND error_code IS NULL "
            "AND retry_reason IS NULL AND cost_amount IS NULL "
            "AND cost_currency IS NULL AND cost_source IS NULL "
            "AND pricing_version IS NULL AND cost_calculated_at IS NULL)",
            name="ck_tuge_model_event_nonterminal_payload",
        ),
        CheckConstraint(
            "dispatch_status = 'DISPATCHED' OR "
            "(input_token_count IS NULL AND output_token_count IS NULL "
            "AND time_to_first_token_ms IS NULL AND cost_amount IS NULL "
            "AND cost_currency IS NULL AND cost_source IS NULL "
            "AND pricing_version IS NULL AND cost_calculated_at IS NULL)",
            name="ck_tuge_model_event_uncertain_usage",
        ),
        CheckConstraint(
            "event_type NOT IN ('MODEL_INVOCATION_STARTED',"
            "'MODEL_INVOCATION_DISPATCH_UNKNOWN') "
            "OR provider_request_id_hash IS NULL",
            name="ck_tuge_model_event_hash_by_fact",
        ),
        CheckConstraint(
            f"((event_type IN ({_TERMINAL_TYPES_SQL}) AND outcome IS NOT NULL) OR "
            f"(event_type NOT IN ({_TERMINAL_TYPES_SQL}) AND outcome IS NULL))",
            name="ck_tuge_model_event_terminal_outcome",
        ),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_OUTCOMES_SQL})",
            name="ck_tuge_model_event_outcome",
        ),
        CheckConstraint(
            "(event_type = 'MODEL_INVOCATION_STARTED' AND "
            "dispatch_status = 'NOT_DISPATCHED') OR "
            "(event_type = 'MODEL_INVOCATION_DISPATCHED' AND "
            "dispatch_status = 'DISPATCHED') OR "
            "(event_type = 'MODEL_INVOCATION_DISPATCH_UNKNOWN' AND "
            "dispatch_status = 'DISPATCH_UNKNOWN') OR "
            "event_type NOT IN ('MODEL_INVOCATION_STARTED',"
            "'MODEL_INVOCATION_DISPATCHED','MODEL_INVOCATION_DISPATCH_UNKNOWN')",
            name="ck_tuge_model_event_dispatch_fact",
        ),
        CheckConstraint(
            "(event_type = 'MODEL_INVOCATION_SUCCEEDED' AND outcome = 'SUCCESS' "
            "AND dispatch_status = 'DISPATCHED') OR "
            "(event_type = 'MODEL_INVOCATION_FAILED' AND outcome IN "
            "('FAILURE','CANCELLED','TIMEOUT','PARTIAL')) OR "
            "(event_type = 'MODEL_INVOCATION_VALIDATION_FAILED' "
            "AND outcome = 'FAILURE' AND dispatch_status = 'DISPATCHED') OR "
            "(event_type = 'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED' "
            "AND outcome = 'DENIED' AND dispatch_status = 'DISPATCHED') OR "
            "(event_type = 'MODEL_INVOCATION_OUTCOME_UNKNOWN' "
            "AND outcome = 'UNKNOWN' AND dispatch_status <> 'NOT_DISPATCHED') OR "
            "(event_type = 'MODEL_INVOCATION_ABANDONED' "
            "AND outcome = 'ABANDONED') OR "
            f"event_type NOT IN ({_TERMINAL_TYPES_SQL})",
            name="ck_tuge_model_event_terminal_semantics",
        ),
        Index(
            "uq_tuge_model_event_started",
            "invocation_id",
            unique=True,
            postgresql_where=text("event_type = 'MODEL_INVOCATION_STARTED'"),
            sqlite_where=text("event_type = 'MODEL_INVOCATION_STARTED'"),
        ),
        Index(
            "uq_tuge_model_event_logical_attempt_started",
            "logical_call_id",
            "attempt_no",
            unique=True,
            postgresql_where=text("event_type = 'MODEL_INVOCATION_STARTED'"),
            sqlite_where=text("event_type = 'MODEL_INVOCATION_STARTED'"),
        ),
        Index(
            "uq_tuge_model_event_dispatch_conclusion",
            "invocation_id",
            unique=True,
            postgresql_where=text(
                "event_type IN ('MODEL_INVOCATION_DISPATCHED',"
                "'MODEL_INVOCATION_DISPATCH_UNKNOWN')"
            ),
            sqlite_where=text(
                "event_type IN ('MODEL_INVOCATION_DISPATCHED',"
                "'MODEL_INVOCATION_DISPATCH_UNKNOWN')"
            ),
        ),
        Index(
            "uq_tuge_model_event_terminal",
            "invocation_id",
            unique=True,
            postgresql_where=text(f"event_type IN ({_TERMINAL_TYPES_SQL})"),
            sqlite_where=text(f"event_type IN ({_TERMINAL_TYPES_SQL})"),
        ),
        Index(
            "idx_tuge_model_event_tenant_occurred",
            "tenant_id",
            "occurred_at",
            "event_id",
        ),
        Index("idx_tuge_model_event_task", "task_id", "occurred_at"),
        Index("idx_tuge_model_event_run", "run_id", "occurred_at"),
        Index("idx_tuge_model_event_request", "request_id", "occurred_at"),
        Index("idx_tuge_model_event_trace", "trace_id", "occurred_at"),
        Index("uq_tuge_model_event_server_sequence", "server_sequence", unique=True),
    )

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    server_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    logical_call_id: Mapped[str] = mapped_column(String(80), nullable=False)
    invocation_id: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    dispatch_status: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_from_invocation_id: Mapped[str | None] = mapped_column(
        String(80)
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(120))
    feature_code: Mapped[str] = mapped_column(String(120), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(80))
    run_id: Mapped[str | None] = mapped_column(String(80))
    stage_id: Mapped[str | None] = mapped_column(String(120))
    request_id: Mapped[str | None] = mapped_column(String(120))
    trace_id: Mapped[str | None] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_request_id_hash: Mapped[str | None] = mapped_column(String(64))
    model_pack_id: Mapped[str | None] = mapped_column(String(120))
    model_pack_version: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    deployment_name: Mapped[str | None] = mapped_column(String(160))
    provider_region: Mapped[str | None] = mapped_column(String(80))
    privacy_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    route_type: Mapped[str] = mapped_column(String(16), nullable=False)
    model_config_id: Mapped[str | None] = mapped_column(String(120))
    input_token_count: Mapped[int | None] = mapped_column(BigInteger)
    output_token_count: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(BigInteger)
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(120))
    retry_reason: Mapped[str | None] = mapped_column(String(120))
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    cost_source: Mapped[str | None] = mapped_column(String(16))
    pricing_version: Mapped[str | None] = mapped_column(String(80))
    cost_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_version: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )


class ModelInvocationProjectionEntity(ModelObservabilityBase):
    __tablename__ = "tuge_model_invocation_projection"
    __table_args__ = (
        UniqueConstraint(
            "logical_call_id",
            "attempt_no",
            name="uq_tuge_model_projection_logical_attempt",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_tuge_model_projection_attempt"),
        CheckConstraint(
            "projection_version >= 1", name="ck_tuge_model_projection_version"
        ),
        CheckConstraint("started_sequence >= 1", name="ck_tuge_model_projection_started_sequence"),
        CheckConstraint(
            "dispatch_sequence IS NULL OR dispatch_sequence > started_sequence",
            name="ck_tuge_model_projection_dispatch_sequence",
        ),
        CheckConstraint(
            "terminal_sequence IS NULL OR terminal_sequence > started_sequence",
            name="ck_tuge_model_projection_terminal_sequence",
        ),
        CheckConstraint(
            "terminal_sequence IS NULL OR dispatch_sequence IS NULL "
            "OR terminal_sequence > dispatch_sequence",
            name="ck_tuge_model_projection_causal_sequence",
        ),
        CheckConstraint(
            "dispatch_status IN ('NOT_DISPATCHED','DISPATCHED','DISPATCH_UNKNOWN')",
            name="ck_tuge_model_projection_dispatch_status",
        ),
        CheckConstraint(
            "lifecycle_status IN ('RUNNING','TERMINAL')",
            name="ck_tuge_model_projection_lifecycle",
        ),
        CheckConstraint(
            "(lifecycle_status = 'RUNNING' AND outcome IS NULL AND finished_at IS NULL) "
            "OR (lifecycle_status = 'TERMINAL' AND outcome IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="ck_tuge_model_projection_terminal",
        ),
        CheckConstraint(
            "fallback_from_invocation_id IS NULL OR "
            "fallback_from_invocation_id <> invocation_id",
            name="ck_tuge_model_projection_fallback",
        ),
        CheckConstraint(
            "input_token_count IS NULL OR input_token_count >= 0",
            name="ck_tuge_model_projection_input_tokens",
        ),
        CheckConstraint(
            "output_token_count IS NULL OR output_token_count >= 0",
            name="ck_tuge_model_projection_output_tokens",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_tuge_model_projection_latency",
        ),
        CheckConstraint(
            "time_to_first_token_ms IS NULL OR time_to_first_token_ms >= 0",
            name="ck_tuge_model_projection_ttft",
        ),
        CheckConstraint(
            "cost_amount IS NULL OR cost_amount >= 0",
            name="ck_tuge_model_projection_cost",
        ),
        CheckConstraint(
            _CURRENCY_SQL,
            name="ck_tuge_model_projection_currency",
        ),
        CheckConstraint(
            "cost_source IS NULL OR cost_source IN ('PROVIDER','ESTIMATED')",
            name="ck_tuge_model_projection_cost_source",
        ),
        CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL "
            "AND cost_source IS NULL AND pricing_version IS NULL "
            "AND cost_calculated_at IS NULL) "
            "OR (cost_amount IS NOT NULL AND cost_currency IS NOT NULL "
            "AND cost_source IS NOT NULL)",
            name="ck_tuge_model_projection_cost_metadata",
        ),
        CheckConstraint(
            _PROVIDER_HASH_SQL,
            name="ck_tuge_model_projection_provider_request_hash",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_tuge_model_projection_time_order",
        ),
        CheckConstraint(
            "time_to_first_token_ms IS NULL OR latency_ms IS NULL "
            "OR time_to_first_token_ms <= latency_ms",
            name="ck_tuge_model_projection_ttft_latency",
        ),
        CheckConstraint(
            "lifecycle_status = 'TERMINAL' OR "
            "(outcome IS NULL AND error_code IS NULL AND retry_reason IS NULL "
            "AND input_token_count IS NULL AND output_token_count IS NULL "
            "AND latency_ms IS NULL AND time_to_first_token_ms IS NULL "
            "AND cost_amount IS NULL AND cost_currency IS NULL "
            "AND cost_source IS NULL AND pricing_version IS NULL "
            "AND cost_calculated_at IS NULL)",
            name="ck_tuge_model_projection_running_payload",
        ),
        CheckConstraint(
            "dispatch_status = 'DISPATCHED' OR "
            "(input_token_count IS NULL AND output_token_count IS NULL "
            "AND time_to_first_token_ms IS NULL AND cost_amount IS NULL "
            "AND cost_currency IS NULL AND cost_source IS NULL "
            "AND pricing_version IS NULL AND cost_calculated_at IS NULL)",
            name="ck_tuge_model_projection_uncertain_usage",
        ),
        CheckConstraint(
            "retry_reason IS NULL OR retry_reason IN "
            "('PROVIDER_RETRY','OUTPUT_REPAIR','GUARDRAIL_RETRY')",
            name="ck_tuge_model_projection_retry_reason",
        ),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_OUTCOMES_SQL})",
            name="ck_tuge_model_projection_outcome",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome NOT IN ('SUCCESS','DENIED') "
            "OR dispatch_status = 'DISPATCHED'",
            name="ck_tuge_model_projection_dispatch_outcome",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome <> 'UNKNOWN' "
            "OR dispatch_status <> 'NOT_DISPATCHED'",
            name="ck_tuge_model_projection_unknown_dispatch",
        ),
        Index(
            "idx_tuge_model_projection_tenant_started",
            "tenant_id",
            "started_at",
            "invocation_id",
        ),
        Index(
            "idx_tuge_model_projection_tenant_dispatch_started",
            "tenant_id",
            "dispatch_status",
            "started_at",
            "invocation_id",
        ),
        Index("idx_tuge_model_projection_task", "task_id", "started_at"),
        Index("idx_tuge_model_projection_run", "run_id", "started_at"),
        Index("idx_tuge_model_projection_trace", "trace_id", "started_at"),
    )

    invocation_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    projection_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    started_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_sequence: Mapped[int | None] = mapped_column(BigInteger)
    terminal_sequence: Mapped[int | None] = mapped_column(BigInteger)
    data_as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    logical_call_id: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    fallback_from_invocation_id: Mapped[str | None] = mapped_column(
        String(80),
        ForeignKey(
            "tuge_model_invocation_projection.invocation_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(120))
    feature_code: Mapped[str] = mapped_column(String(120), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(80))
    run_id: Mapped[str | None] = mapped_column(String(80))
    stage_id: Mapped[str | None] = mapped_column(String(120))
    request_id: Mapped[str | None] = mapped_column(String(120))
    trace_id: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False)
    dispatch_status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(120))
    retry_reason: Mapped[str | None] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_request_id_hash: Mapped[str | None] = mapped_column(String(64))
    model_pack_id: Mapped[str | None] = mapped_column(String(120))
    model_pack_version: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    deployment_name: Mapped[str | None] = mapped_column(String(160))
    provider_region: Mapped[str | None] = mapped_column(String(80))
    privacy_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    route_type: Mapped[str] = mapped_column(String(16), nullable=False)
    model_config_id: Mapped[str | None] = mapped_column(String(120))
    input_token_count: Mapped[int | None] = mapped_column(BigInteger)
    output_token_count: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(BigInteger)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    cost_source: Mapped[str | None] = mapped_column(String(16))
    pricing_version: Mapped[str | None] = mapped_column(String(80))
    cost_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_version: Mapped[str | None] = mapped_column(String(80))
