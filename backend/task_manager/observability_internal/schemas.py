from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TASK_STATUSES = {
    "created",
    "pending",
    "queued",
    "running",
    "waiting_human",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
}
RUN_STATUSES = {
    "pending",
    "queued",
    "running",
    "waiting_human",
    "succeeded",
    "failed",
    "cancelled",
    "archived",
}
STAGE_STATUSES = {
    "pending",
    "running",
    "waiting_human",
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
}
OUTCOMES = {
    "SUCCESS",
    "FAILURE",
    "DENIED",
    "CANCELLED",
    "TIMEOUT",
    "PARTIAL",
    "UNKNOWN",
    "ABANDONED",
}


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class Actor(ApiModel):
    type: Literal["USER", "SERVICE", "SYSTEM", "ANONYMOUS", "UNKNOWN"]
    id: str | None = Field(default=None, min_length=1, max_length=120)


class Subject(ApiModel):
    type: str = Field(min_length=1, max_length=80)
    id: str | None = Field(default=None, min_length=1, max_length=120)


class MetadataField(ApiModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=512)
    masked: bool


class InternalRun(ApiModel):
    run_id: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=32)
    attempt_no: int = Field(ge=1, strict=True)
    started_at: datetime | None
    finished_at: datetime | None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in RUN_STATUSES:
            raise ValueError("invalid Run status")
        return value

    @model_validator(mode="after")
    def validate_time_order(self):
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("Run finishedAt precedes startedAt")
        return self


class InternalTask(ApiModel):
    task_id: str = Field(min_length=1, max_length=80)
    projection_version: int = Field(ge=1, strict=True)
    data_as_of: datetime
    feature_code: str = Field(min_length=1, max_length=120)
    tenant_id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    created_at: datetime
    ingested_at: datetime
    schema_version: int = Field(default=1, ge=1, strict=True)
    initiator: Actor | None = None
    subject: Subject | None = None
    stage: str | None = Field(default=None, min_length=1, max_length=120)
    progress: float | None = Field(default=None, ge=0, le=100, strict=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, strict=True)
    retry_count: int = Field(default=0, ge=0, strict=True)
    privacy_mode: Literal["STANDARD", "PRIVATE"] | None = None
    route_type: Literal["LOCAL", "EXTERNAL"] | None = None
    current_run: InternalRun | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=120)
    trace_ids: list[str] = Field(default_factory=list, max_length=128)
    error_code: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in TASK_STATUSES:
            raise ValueError("invalid Task status")
        return value

    @field_validator("trace_ids")
    @classmethod
    def validate_trace_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not TRACE_ID_PATTERN.fullmatch(item) for item in value
        ):
            raise ValueError("invalid traceIds")
        return value

    @model_validator(mode="after")
    def validate_time_order(self):
        if self.ingested_at < self.created_at:
            raise ValueError("Task ingestedAt precedes createdAt")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("Task finishedAt precedes startedAt")
        return self


class InternalStage(ApiModel):
    stage_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=32)
    sequence: int = Field(ge=1, strict=True)
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None = Field(default=None, ge=0, strict=True)
    retry_count: int = Field(default=0, ge=0, strict=True)
    error_code: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in STAGE_STATUSES:
            raise ValueError("invalid Stage status")
        return value

    @model_validator(mode="after")
    def validate_time_order(self):
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("Stage finishedAt precedes startedAt")
        return self


class InternalTaskEvent(ApiModel):
    event_id: str = Field(min_length=1, max_length=80)
    sequence: int = Field(ge=1, strict=True)
    event_type: str = Field(min_length=1, max_length=80)
    schema_version: int = Field(ge=1, strict=True)
    tenant_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    ingested_at: datetime
    display_code: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str | None = Field(default=None, min_length=1, max_length=80)
    stage_id: str | None = Field(default=None, min_length=1, max_length=80)
    agent_name: str | None = Field(default=None, min_length=1, max_length=80)
    tool_name: str | None = Field(default=None, min_length=1, max_length=120)
    request_id: str | None = Field(default=None, min_length=1, max_length=120)
    trace_id: str | None = None
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"] | None = None
    outcome: (
        Literal[
            "SUCCESS",
            "FAILURE",
            "DENIED",
            "CANCELLED",
            "TIMEOUT",
            "PARTIAL",
            "UNKNOWN",
            "ABANDONED",
        ]
        | None
    ) = None
    error_code: str | None = Field(default=None, min_length=1, max_length=120)
    duration_ms: int | None = Field(default=None, ge=0, strict=True)
    metadata: list[MetadataField] = Field(default_factory=list, max_length=128)

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str | None) -> str | None:
        if value is not None and not TRACE_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid traceId")
        return value

    @model_validator(mode="after")
    def validate_time_order(self):
        if self.ingested_at < self.occurred_at:
            raise ValueError("Task Event ingestedAt precedes occurredAt")
        return self


class InternalSecurityEvent(ApiModel):
    event_id: str = Field(min_length=1, max_length=80)
    audit_action_id: str = Field(min_length=1, max_length=96)
    audit_layer: Literal["JAVA_GATEWAY", "PYTHON_EXECUTION"]
    scope_type: Literal["TENANT", "SYSTEM"]
    schema_version: int = Field(ge=1, strict=True)
    service: str = Field(min_length=1, max_length=80)
    occurred_at: datetime
    ingested_at: datetime
    category: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=120)
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    cross_tenant: bool
    outcome: Literal[
        "SUCCESS",
        "FAILURE",
        "DENIED",
        "CANCELLED",
        "TIMEOUT",
        "PARTIAL",
        "UNKNOWN",
        "ABANDONED",
    ]
    display_code: str = Field(min_length=1, max_length=120)
    access_session_id: str | None = Field(default=None, min_length=1, max_length=96)
    parent_audit_event_id: str | None = Field(default=None, min_length=1, max_length=96)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=64)
    actor: Actor | None = None
    subject: Subject | None = None
    missing_context_reason: str | None = Field(
        default=None, min_length=1, max_length=512
    )
    reason_code: str | None = Field(default=None, min_length=1, max_length=80)
    source_ip_masked: str | None = Field(default=None, max_length=80)
    request_id: str | None = Field(default=None, min_length=1, max_length=120)
    trace_id: str | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=120)
    metadata: list[MetadataField] = Field(default_factory=list, max_length=128)

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str | None) -> str | None:
        if value is not None and not TRACE_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid traceId")
        return value

    @model_validator(mode="after")
    def validate_context(self):
        if self.occurred_at - self.ingested_at > timedelta(seconds=5):
            raise ValueError("Security Event occurredAt exceeds allowed clock skew")
        if self.scope_type == "TENANT" and self.tenant_id is None:
            raise ValueError("TENANT Security Event requires tenantId")
        if (
            self.tenant_id is None
            or self.actor is None
            or self.actor.id is None
            or self.subject is None
            or self.subject.id is None
        ) and self.missing_context_reason is None:
            raise ValueError("incomplete Security context needs a reason")
        return self


class PageInfo(ApiModel):
    next_cursor: str | None = Field(default=None, min_length=16, max_length=4096)
    has_more: bool
    limit: int = Field(ge=1, le=200, strict=True)


class SourceWatermark(ApiModel):
    query_snapshot_id: str = Field(min_length=1, max_length=80)
    snapshot_to: datetime
    expires_at: datetime
    snapshot_mode: Literal[
        "APPEND_ONLY_HIGH_WATERMARK",
        "MATERIALIZED_RESULT_SET",
    ]
    high_watermark_handle: str = Field(
        min_length=14,
        max_length=84,
        pattern=r"^hwm_[A-Za-z0-9_-]{10,80}$",
    )
    max_ingested_at: datetime | None = None
    max_sequence: int | None = Field(default=None, ge=1, strict=True)
    max_event_id: str | None = Field(default=None, min_length=1, max_length=80)
    data_through: datetime | None = None


class InternalTaskList(ApiModel):
    data: list[InternalTask]
    page: PageInfo
    watermark: SourceWatermark


class InternalTaskEventList(ApiModel):
    data: list[InternalTaskEvent]
    page: PageInfo
    watermark: SourceWatermark


class InternalStageList(ApiModel):
    data: list[InternalStage]


class InternalSecurityEventList(ApiModel):
    data: list[InternalSecurityEvent]
    page: PageInfo
    watermark: SourceWatermark


class StreamReset(ApiModel):
    code: Literal["EVENT_STREAM_RESET_REQUIRED"]
    run_id: str = Field(min_length=1, max_length=80)
    last_available_sequence: int = Field(ge=0, strict=True)


class ErrorResponse(ApiModel):
    code: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=128)
    retryable: bool = False


def as_api_payload(model: BaseModel | list[BaseModel] | dict[str, Any]) -> Any:
    if isinstance(model, BaseModel):
        return model.model_dump(by_alias=True, mode="json")
    if isinstance(model, list):
        return [as_api_payload(item) for item in model]
    return model
