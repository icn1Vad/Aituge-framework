from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    CostSource,
    DispatchStatus,
    InvocationLifecycleStatus,
    InvocationOutcome,
    PrivacyMode,
    RouteType,
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        from_attributes=True,
    )


class MetadataField(ContractModel):
    key: str = Field(max_length=64)
    value: str = Field(max_length=512)
    masked: bool


class InternalModelInvocation(ContractModel):
    invocation_id: str
    projection_version: int = Field(ge=1)
    data_as_of: datetime
    logical_call_id: str
    attempt_no: int = Field(ge=1)
    fallback_from_invocation_id: str | None = None
    tenant_id: str
    feature_code: str
    task_id: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    ingested_at: datetime
    lifecycle_status: InvocationLifecycleStatus
    dispatch_status: DispatchStatus
    outcome: InvocationOutcome | None = None
    error_code: str | None = None
    provider: str
    provider_request_id_hash: str | None = None
    model_pack_id: str | None = None
    model_pack_version: str | None = None
    model_name: str
    deployment_name: str | None = None
    provider_region: str | None = None
    privacy_mode: PrivacyMode
    route_type: RouteType
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    cost_amount: float | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_source: CostSource | None = None
    pricing_version: str | None = None
    cost_calculated_at: datetime | None = None


class InternalModelInvocationEvent(ContractModel):
    event_id: str
    event_type: str
    schema_version: int = Field(ge=1)
    logical_call_id: str
    invocation_id: str
    attempt_no: int = Field(ge=1)
    fallback_from_invocation_id: str | None = None
    tenant_id: str
    feature_code: str
    task_id: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    occurred_at: datetime
    ingested_at: datetime
    dispatch_status: DispatchStatus
    provider: str
    provider_request_id_hash: str | None = None
    model_pack_id: str | None = None
    model_pack_version: str | None = None
    model_name: str
    deployment_name: str | None = None
    provider_region: str | None = None
    privacy_mode: PrivacyMode
    route_type: RouteType
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    cost_amount: float | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_source: CostSource | None = None
    pricing_version: str | None = None
    cost_calculated_at: datetime | None = None
    outcome: InvocationOutcome | None = None
    error_code: str | None = None
    metadata: list[MetadataField] = Field(default_factory=list)


class InternalModelInvocationDetail(ContractModel):
    invocation: InternalModelInvocation
    events: list[InternalModelInvocationEvent]


class Percentiles(ContractModel):
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)
    p99: float = Field(ge=0)


class BreakdownItem(ContractModel):
    key: str
    count: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)


class CurrencyAmount(ContractModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: float = Field(ge=0)


class ModelTimeSeriesPoint(ContractModel):
    bucket_start: datetime
    logical_call_count: int = Field(ge=0)
    dispatch_attempt_count: int = Field(ge=0)
    dispatched_request_count: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    p95_latency_ms: float = Field(ge=0)


class InternalModelSummary(ContractModel):
    logical_call_count: int = Field(ge=0)
    dispatch_attempt_count: int = Field(ge=0)
    dispatched_request_count: int = Field(ge=0)
    not_dispatched_count: int = Field(ge=0)
    dispatch_unknown_count: int = Field(ge=0)
    active_attempt_count: int = Field(ge=0)
    logical_success_rate: float = Field(ge=0, le=1)
    attempt_success_rate: float = Field(ge=0, le=1)
    retry_rate: float = Field(ge=0, le=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    costs: list[CurrencyAmount]
    latency_percentiles: Percentiles | None = None
    ttft_percentiles: Percentiles | None = None
    time_series: list[ModelTimeSeriesPoint] = Field(default_factory=list)
    error_breakdown: list[BreakdownItem] = Field(default_factory=list)
    provider_breakdown: list[BreakdownItem] = Field(default_factory=list)
    model_breakdown: list[BreakdownItem] = Field(default_factory=list)
    route_type_breakdown: list[BreakdownItem] = Field(default_factory=list)
    privacy_mode_breakdown: list[BreakdownItem] = Field(default_factory=list)


class PageInfo(ContractModel):
    next_cursor: str | None = None
    has_more: bool
    limit: int = Field(ge=1)


class SourceWatermark(ContractModel):
    query_snapshot_id: str
    snapshot_to: datetime
    expires_at: datetime
    snapshot_mode: str
    max_ingested_at: datetime | None = None
    max_sequence: int | None = Field(default=None, ge=1)
    max_event_id: str | None = None
    high_watermark_handle: str = Field(
        min_length=14,
        max_length=84,
        pattern=r"^hwm_[A-Za-z0-9_-]{10,80}$",
    )
    data_through: datetime | None = None


class InternalModelInvocationList(ContractModel):
    data: list[InternalModelInvocation]
    page: PageInfo
    watermark: SourceWatermark


class InternalModelEventList(ContractModel):
    data: list[InternalModelInvocationEvent]
    page: PageInfo
    watermark: SourceWatermark


class ErrorResponse(ContractModel):
    code: str
    request_id: str
    retryable: bool = False


def metadata_fields(value: dict[str, Any] | None) -> list[MetadataField]:
    fields: list[MetadataField] = []
    for key, item in sorted((value or {}).items()):
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError("model event metadata must contain string pairs")
        fields.append(MetadataField(key=key, value=item, masked=False))
    return fields
