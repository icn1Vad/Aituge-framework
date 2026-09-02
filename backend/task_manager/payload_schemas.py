from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gateway.models import GatewayResourceRef


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskPayloadBase(StrictPayload):
    resource_refs: list[GatewayResourceRef] = Field(default_factory=list)
    validated_resource_refs: list[dict[str, Any]] = Field(default_factory=list)


class AiSearchChatInput(TaskPayloadBase):
    message: str = Field(min_length=1)
    search_goal: str = ""
    max_results: int = Field(default=5, ge=1, le=10)
    context: dict[str, Any] = Field(default_factory=dict)


class TableAuditInput(TaskPayloadBase):
    rows: list[dict[str, Any]] = Field(min_length=1)
    audit_goal: str = Field(min_length=1)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    failure_policy: Literal["continue", "fail_fast"] = "continue"
    retry_per_item: int = Field(default=0, ge=0, le=3)


class PipelineDemoInput(TaskPayloadBase):
    goal: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    require_human_review: bool = False


class PipelineDemoAnalysis(StrictPayload):
    summary: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)


class PipelineDemoNormalized(StrictPayload):
    summary: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    normalized: Literal[True] = True


class PipelineDemoResult(StrictPayload):
    status: Literal["success", "needs_human_review"]
    summary: str = Field(min_length=1)
    steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)


class AiSearchQueryPlan(StrictPayload):
    user_goal: str
    queries: list[str] = Field(default_factory=list)
    query_reasons: list[str] = Field(default_factory=list)


class AiSearchResultItem(StrictPayload):
    rank: int = Field(ge=1)
    title: str = Field(min_length=1)
    url: str = ""
    source_name: str = ""
    content_excerpt: str = ""
    published_at: str = ""
    relevance_score: float = Field(ge=0, le=1)
    recommendation: Literal["keep", "maybe", "drop"] = "keep"
    reason: str = ""


class AiSearchEvidenceSummary(StrictPayload):
    confirmed: list[str] = Field(default_factory=list)
    weak_or_missing: list[str] = Field(default_factory=list)
    source_quality_notes: list[str] = Field(default_factory=list)


class AiSearchOutput(StrictPayload):
    status: Literal["success", "partial", "search_failed", "needs_clarification"]
    answer: str
    query_plan: AiSearchQueryPlan
    evidence_summary: AiSearchEvidenceSummary | None = None
    results: list[AiSearchResultItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list)


class TableAuditItemOutput(StrictPayload):
    risk_level: Literal["low", "medium", "high", "unknown"]
    passed: bool
    issues: list[Any] = Field(default_factory=list)
    reason: str
    recommended_action: str = ""
    evidence: list[Any] = Field(default_factory=list)


class BatchSummary(StrictPayload):
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class BatchTaskOutput(StrictPayload):
    summary: BatchSummary
    items: list[dict[str, Any]] = Field(default_factory=list)


_INPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "ai_search_chat_input": AiSearchChatInput,
    "table_audit_input": TableAuditInput,
    "pipeline_demo_input": PipelineDemoInput,
}

_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "ai_search_output": AiSearchOutput,
    "table_audit_item_output": TableAuditItemOutput,
    "batch_task_output": BatchTaskOutput,
    "pipeline_demo_analysis": PipelineDemoAnalysis,
    "pipeline_demo_normalized": PipelineDemoNormalized,
    "pipeline_demo_result": PipelineDemoResult,
}


def register_input_schema(name: str, schema: type[BaseModel]) -> None:
    _register_schema(_INPUT_SCHEMAS, name, schema)


def register_output_schema(name: str, schema: type[BaseModel]) -> None:
    _register_schema(_OUTPUT_SCHEMAS, name, schema)


def _register_schema(
    registry: dict[str, type[BaseModel]],
    name: str,
    schema: type[BaseModel],
) -> None:
    if not name:
        raise ValueError("Schema name is required.")
    existing = registry.get(name)
    if existing is not None and existing is not schema:
        if existing.model_json_schema() != schema.model_json_schema():
            raise ValueError(
                f"Schema '{name}' is already registered with a different contract."
            )
    registry[name] = schema


def validate_input_payload(
    schema_name: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not schema_name:
        return payload
    schema = _INPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown input schema '{schema_name}'.")
    try:
        return schema.model_validate(payload).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError(
            f"Input payload does not match schema '{schema_name}': {exc.errors()}"
        ) from exc


def validate_output_payload(
    schema_name: str | None,
    payload: Any,
) -> tuple[bool, dict[str, Any] | None]:
    if not schema_name or payload is None:
        return True, None
    schema = _OUTPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown output schema '{schema_name}'.")
    try:
        schema.model_validate(payload)
        return True, None
    except ValidationError as exc:
        errors = json.loads(
            json.dumps(exc.errors(), ensure_ascii=False, default=str)
        )
        return False, {"schema_name": schema_name, "errors": errors}


def validate_stage_payload(
    schema_name: str | None,
    payload: Any,
) -> dict[str, Any]:
    if not schema_name:
        if not isinstance(payload, dict):
            raise ValueError("Stage payload must be a JSON object.")
        return payload
    schema = _OUTPUT_SCHEMAS.get(schema_name) or _INPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown stage schema '{schema_name}'.")
    try:
        # Stage artifacts are persisted and hashed as JSON immediately after
        # validation. Preserve the schema check while returning JSON-native
        # values (dates, datetimes, UUIDs, enums) rather than Python objects.
        return schema.model_validate(payload).model_dump(
            mode="json",
            exclude_none=True,
        )
    except ValidationError as exc:
        raise ValueError(
            f"Stage payload does not match schema '{schema_name}': {exc.errors()}"
        ) from exc


def get_stage_json_schema(
    schema_name: str | None,
) -> dict[str, Any] | None:
    """Return the registered JSON Schema used to validate one pipeline stage."""

    if not schema_name:
        return None
    schema = _OUTPUT_SCHEMAS.get(schema_name) or _INPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown stage schema '{schema_name}'.")
    return schema.model_json_schema()
