from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .gateway.models import GatewayResourceRef


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskPayloadBase(StrictPayload):
    resource_refs: list[GatewayResourceRef] = Field(default_factory=list)
    validated_resource_refs: list[dict[str, Any]] = Field(default_factory=list)


class MediaScriptGenerateInput(TaskPayloadBase):
    topic: str = Field(min_length=1)
    platform: Literal["douyin"] = "douyin"
    duration_seconds: int = Field(default=60, ge=15, le=300)
    source_brief: str = ""
    expected_output: str = ""
    persona: Optional[str] = None
    account_persona: Optional[str] = None
    materials: list[dict[str, Any]] = Field(default_factory=list)
    comments: list[Any] = Field(default_factory=list)
    manual_direction: Optional[str] = None


class MediaScriptSelectInput(TaskPayloadBase):
    topic: str = Field(min_length=1)
    script_candidates: list[dict[str, Any]] = Field(min_length=1)
    platform: Literal["douyin"] = "douyin"
    duration_seconds: int = Field(default=60, ge=15, le=300)
    source_brief: str = ""
    expected_output: str = ""
    selection_goal: Optional[str] = None


class MediaChatInput(TaskPayloadBase):
    message: Optional[str] = None
    question: Optional[str] = None
    topic: Optional[str] = None
    platform: Literal["douyin"] = "douyin"
    duration_seconds: int = Field(default=60, ge=15, le=300)
    source_brief: str = ""
    expected_output: str = ""
    context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_message_or_question(self):
        if not (self.message or self.question or self.topic):
            raise ValueError("One of message, question, or topic is required.")
        return self


class AiSearchChatInput(TaskPayloadBase):
    message: str = Field(min_length=1)
    search_goal: str = ""
    platform: Literal["web", "douyin", "xiaohongshu", "bilibili", "general"] = "web"
    max_results: int = Field(default=5, ge=1, le=10)
    context: dict[str, Any] = Field(default_factory=dict)


class TableAuditInput(TaskPayloadBase):
    rows: list[dict[str, Any]] = Field(min_length=1)
    audit_goal: str = Field(min_length=1)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    failure_policy: Literal["continue", "fail_fast"] = "continue"
    retry_per_item: int = Field(default=0, ge=0, le=3)
    topic: Optional[str] = None
    platform: Literal["douyin"] = "douyin"
    duration_seconds: int = Field(default=60, ge=15, le=300)
    source_brief: str = ""
    expected_output: str = ""


class MediaScriptOutput(StrictPayload):
    final_script: dict[str, Any]
    readable_script: str
    hermes_agent_result: dict[str, Any]


class AiSearchQueryPlan(StrictPayload):
    user_goal: str
    queries: list[str] = Field(default_factory=list)


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


class AiSearchOutput(StrictPayload):
    status: Literal["success", "partial", "search_failed", "needs_clarification"]
    answer: str
    query_plan: AiSearchQueryPlan
    results: list[AiSearchResultItem] = Field(default_factory=list)
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
    "media_script_generate_input": MediaScriptGenerateInput,
    "media_script_select_input": MediaScriptSelectInput,
    "media_chat_input": MediaChatInput,
    "ai_search_chat_input": AiSearchChatInput,
    "table_audit_input": TableAuditInput,
}

_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "media_script_output": MediaScriptOutput,
    "ai_search_output": AiSearchOutput,
    "table_audit_item_output": TableAuditItemOutput,
    "batch_task_output": BatchTaskOutput,
}


def validate_input_payload(schema_name: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    if not schema_name:
        return payload
    schema = _INPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown input schema '{schema_name}'.")
    try:
        return schema.model_validate(payload).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError(f"Input payload does not match schema '{schema_name}': {exc.errors()}") from exc


def validate_output_payload(schema_name: str | None, payload: Any) -> tuple[bool, dict[str, Any] | None]:
    if not schema_name or payload is None:
        return True, None
    schema = _OUTPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown output schema '{schema_name}'.")
    try:
        schema.model_validate(payload)
        return True, None
    except ValidationError as exc:
        return False, {"schema_name": schema_name, "errors": exc.errors()}
