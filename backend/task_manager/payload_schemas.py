from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .gateway.models import GatewayResourceRef


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskPayloadBase(StrictPayload):
    resource_refs: list[GatewayResourceRef] = Field(default_factory=list)
    validated_resource_refs: list[dict[str, Any]] = Field(default_factory=list)


class MediaScriptGenerateInput(TaskPayloadBase):
    topic: str = Field(min_length=1)
    topic_card_id: Optional[str] = None
    source_material_id: Optional[str] = None
    topic_card: dict[str, Any] = Field(default_factory=dict)
    platform: Literal["douyin"] = "douyin"
    duration_seconds: int = Field(default=60, ge=15, le=300)
    source_brief: str = ""
    expected_output: str = ""
    persona: Optional[str] = None
    account_persona: Optional[str] = None
    materials: list[dict[str, Any]] = Field(default_factory=list)
    comments: list[Any] = Field(default_factory=list)
    manual_direction: Optional[str] = None
    persona_id: Optional[str] = None
    parent_script_id: Optional[str] = None
    conversation_thread_id: Optional[str] = None
    require_human_review: bool = False
    context: dict[str, Any] = Field(default_factory=dict)
    revision_mode: bool = False
    base_script_id: Optional[str] = None
    base_artifact_id: Optional[str] = None
    proposal_artifact_id: Optional[str] = None
    previous_script: dict[str, Any] = Field(default_factory=dict)
    change_proposal: dict[str, Any] = Field(default_factory=dict)
    preserve_fields: list[str] = Field(default_factory=list)
    parent_task_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_revision_context(self):
        if not self.revision_mode:
            return self
        missing = []
        if not self.base_script_id:
            missing.append("base_script_id")
        if not self.proposal_artifact_id:
            missing.append("proposal_artifact_id")
        if not self.previous_script:
            missing.append("previous_script")
        if not self.change_proposal:
            missing.append("change_proposal")
        if missing:
            raise ValueError(f"Revision mode requires: {', '.join(missing)}.")
        return self


class MediaScriptChangeProposalInput(TaskPayloadBase):
    message: str = Field(min_length=1)
    base_script_id: str = Field(min_length=1)
    base_artifact_id: Optional[str] = None
    conversation_thread_id: Optional[str] = None
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    current_script: dict[str, Any] = Field(default_factory=dict)
    topic_card: dict[str, Any] = Field(default_factory=dict)
    persona: dict[str, Any] = Field(default_factory=dict)
    user_constraints: dict[str, Any] = Field(default_factory=dict)


class MediaScriptProposalChange(StrictPayload):
    field: str = Field(min_length=1)
    instruction: str = Field(min_length=1)


class MediaScriptChangeProposalOutput(StrictPayload):
    status: Literal["pending_confirmation", "needs_clarification"]
    summary: str = Field(min_length=1)
    reason: str = ""
    target_fields: list[str] = Field(default_factory=list)
    changes: list[MediaScriptProposalChange] = Field(default_factory=list)
    preserve_fields: list[str] = Field(default_factory=list)
    storyboard_regeneration_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    clarification_question: str = ""

    @model_validator(mode="after")
    def validate_status_contract(self):
        if self.status == "pending_confirmation" and not (self.target_fields and self.changes):
            raise ValueError("A pending proposal requires target_fields and changes.")
        if self.status == "needs_clarification" and not self.clarification_question:
            raise ValueError("A clarification question is required when status is needs_clarification.")
        return self


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


class MediaScriptMainAgentInput(TaskPayloadBase):
    workspace_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    operation: Literal["generate", "interact"] = "interact"
    context: dict[str, Any] = Field(default_factory=dict)


class MediaScriptWorkspaceOutput(StrictPayload):
    workspace_id: str = Field(min_length=1)
    script_text: str = ""
    storyboard_text: str = ""
    response: str = ""


class AiSearchChatInput(TaskPayloadBase):
    message: str = Field(min_length=1)
    search_goal: str = ""
    platform: Literal["web", "douyin", "xiaohongshu", "bilibili", "general"] = "web"
    max_results: int = Field(default=5, ge=1, le=10)
    context: dict[str, Any] = Field(default_factory=dict)


class MediaTopicSearchInput(TaskPayloadBase):
    message: Optional[str] = None
    topic_query: Optional[str] = None
    search_goal: str = ""
    platform: Literal["douyin", "wechat_video", "xiaohongshu", "bilibili", "general"] = "douyin"
    max_results: int = Field(default=5, ge=1, le=10)
    max_topics: int = Field(default=5, ge=1, le=8)
    business_axes: list[str] = Field(default_factory=list)
    account_context: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_message_or_topic_query(self):
        if not (self.message or self.topic_query):
            raise ValueError("One of message or topic_query is required.")
        return self


class DouyinAccountReportInput(TaskPayloadBase):
    account_id: str = ""
    account_name: Optional[str] = None
    platform: Literal["douyin"] = "douyin"
    data_source: Literal["payload", "legacy_douyin_api"] = "payload"
    legacy_api_base_url: Optional[str] = None
    content_limit: int = Field(default=50, ge=1, le=200)
    analysis_scope: Literal["all_data", "month", "custom_range"] = "all_data"
    month: Optional[str] = None
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    report_depth: Literal["standard", "deep"] = "deep"
    report_goal: str = "Generate a complete Douyin account operations analysis report from all available data."
    report_context: dict[str, Any] = Field(default_factory=dict)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
    content_items: list[dict[str, Any]] = Field(default_factory=list)
    top_contents: list[dict[str, Any]] = Field(default_factory=list)
    low_contents: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    operator_notes: str = ""

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.analysis_scope == "month" and not self.month:
            raise ValueError("month is required when analysis_scope is 'month'.")
        if self.analysis_scope == "custom_range" and not (self.date_start and self.date_end):
            raise ValueError("date_start and date_end are required when analysis_scope is 'custom_range'.")
        if self.data_source == "legacy_douyin_api":
            return self
        if not (self.report_context or self.metrics_summary or self.content_items or self.top_contents):
            raise ValueError(
                "At least one of report_context, metrics_summary, content_items, or top_contents is required."
            )
        return self


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


SmartFillGroupId = Literal["project", "company", "financial", "risk", "analysis"]
SmartFillSkillPackage = Literal[
    "smart-fill-project-package",
    "smart-fill-company-package",
    "smart-fill-financial-package",
    "smart-fill-risk-package",
    "smart-fill-analysis-package",
]

SMART_FILL_GROUP_PACKAGES = {
    "project": "smart-fill-project-package",
    "company": "smart-fill-company-package",
    "financial": "smart-fill-financial-package",
    "risk": "smart-fill-risk-package",
    "analysis": "smart-fill-analysis-package",
}


class SmartFillExtractItem(StrictPayload):
    id: SmartFillGroupId
    group_id: SmartFillGroupId
    skill_package: SmartFillSkillPackage
    field_ids: list[str] = Field(min_length=1)
    field_specs: list[dict[str, Any]] = Field(default_factory=list)
    extraction_instructions: str = ""

    @model_validator(mode="after")
    def validate_group_package(self):
        if self.id != self.group_id:
            raise ValueError("Smart fill item id must match group_id.")
        expected = SMART_FILL_GROUP_PACKAGES[self.group_id]
        if self.skill_package != expected:
            raise ValueError(
                f"Smart fill group '{self.group_id}' must use skill package '{expected}'."
            )
        if len(self.field_ids) != len(set(self.field_ids)):
            raise ValueError(f"Smart fill group '{self.group_id}' contains duplicate field_ids.")
        return self


class SmartFillExtractInput(TaskPayloadBase):
    document_ids: list[str] = Field(default_factory=list)
    items: list[SmartFillExtractItem] = Field(min_length=5, max_length=5)
    max_concurrency: Literal[5] = 5
    failure_policy: Literal["continue", "fail_fast"] = "continue"
    retry_per_item: int = Field(default=1, ge=0, le=3)
    source_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_complete_group_set(self):
        expected = set(SMART_FILL_GROUP_PACKAGES)
        actual = {item.group_id for item in self.items}
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                f"Smart fill task must contain all five groups; missing={missing}, unexpected={unexpected}."
            )
        all_field_ids = [field_id for item in self.items for field_id in item.field_ids]
        if len(all_field_ids) != len(set(all_field_ids)):
            raise ValueError("Smart fill field_ids must not overlap across extraction groups.")
        return self


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


class MediaScriptOutput(StrictPayload):
    final_script: dict[str, Any]
    readable_script: str
    hermes_agent_result: dict[str, Any]
    workflow_state: dict[str, Any] = Field(default_factory=dict)
    generation_meta: dict[str, Any] = Field(default_factory=dict)


class MediaScriptContextBundle(StrictPayload):
    topic_card: dict[str, Any]
    source_brief: dict[str, Any] = Field(default_factory=dict)
    current_persona: dict[str, Any] = Field(default_factory=dict)
    persona_context: dict[str, Any] = Field(default_factory=dict)
    material_full: dict[str, Any] = Field(default_factory=dict)
    material_comments: dict[str, Any] = Field(default_factory=dict)
    script_stack_recommendation: dict[str, Any] = Field(default_factory=dict)
    material_analysis: dict[str, Any] = Field(default_factory=dict)
    product_intent: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, Any] = Field(default_factory=dict)
    user_constraints: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class MediaScriptResearchControversy(StrictPayload):
    issue: str = Field(min_length=1)
    detail: str = ""
    severity: str = ""


class MediaScriptRecommendedAngle(StrictPayload):
    main_angle: str = Field(min_length=1)
    rationale: str = ""
    suggested_structure: str = ""
    persona_fit: str = ""
    formula_suggestion: str = ""


class MediaScriptResearchRisk(StrictPayload):
    risk: str = Field(min_length=1)
    detail: str = ""
    mitigation: str = ""


class MediaScriptResearchBundle(StrictPayload):
    topic_summary: str = Field(min_length=1)
    key_facts: list[dict[str, Any]] = Field(default_factory=list)
    usable_materials: list[dict[str, Any]] = Field(default_factory=list)
    audience_questions: list[str] = Field(default_factory=list)
    controversies: list[str | MediaScriptResearchControversy] = Field(default_factory=list)
    source_evidence: list[dict[str, Any]] = Field(default_factory=list)
    recommended_angle: str | MediaScriptRecommendedAngle
    risks: list[str | MediaScriptResearchRisk] = Field(default_factory=list)


class MediaScriptResearchContext(StrictPayload):
    topic_card: dict[str, Any]
    source_brief: dict[str, Any] = Field(default_factory=dict)
    material_full: dict[str, Any] = Field(default_factory=dict)
    material_comments: dict[str, Any] = Field(default_factory=dict)
    current_persona: dict[str, Any] = Field(default_factory=dict)
    persona_context: dict[str, Any] = Field(default_factory=dict)
    user_constraints: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class MediaScriptWriterDraft(StrictPayload):
    final_script: dict[str, Any]
    readable_script: str = Field(min_length=1)
    hermes_agent_result: dict[str, Any]


class MediaStoryboardDraft(StrictPayload):
    storyboard: list[dict[str, Any]] = Field(default_factory=list)
    storyboard_plan: dict[str, Any] = Field(default_factory=dict)
    visual_direction: list[str] | str = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("storyboard_plan", mode="before")
    @classmethod
    def normalize_storyboard_plan(cls, value):
        if isinstance(value, str):
            return {"summary": value}
        return value

    @field_validator("warnings", mode="before")
    @classmethod
    def normalize_warnings(cls, value):
        if isinstance(value, str):
            return [value]
        return value


class MediaScriptCheckResult(StrictPayload):
    passed: bool
    high_risk: bool
    score: float = Field(ge=0, le=100)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class MediaScriptReviewResult(StrictPayload):
    recommendation: Literal["pass", "needs_human_review", "reject"]
    summary: str = Field(min_length=1)
    compliance_findings: list[str | dict[str, Any]] = Field(default_factory=list)
    quality_findings: list[str | dict[str, Any]] = Field(default_factory=list)
    storyboard_findings: list[str | dict[str, Any]] = Field(default_factory=list)
    revise_instruction: str = ""


class AiSearchQueryPlan(StrictPayload):
    user_goal: str
    queries: list[str] = Field(default_factory=list)
    mode: Optional[Literal["specific_search", "hotspot_discovery", "general_search_chat"]] = None
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


class AiSearchBusinessBridge(StrictPayload):
    level: Literal["none", "soft", "medium", "strong"] = "none"
    axis: str = ""
    placement: str = ""


class AiSearchTopicSuggestion(StrictPayload):
    topic_title: str = Field(min_length=1)
    topic_intro: str = ""
    content_direction: str = ""
    writing_outline: list[str] = Field(default_factory=list)
    business_bridge: AiSearchBusinessBridge = Field(default_factory=AiSearchBusinessBridge)
    why_now: str = ""
    supporting_result_ranks: list[int] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class AiSearchOutput(StrictPayload):
    status: Literal["success", "partial", "search_failed", "needs_clarification"]
    answer: str
    query_plan: AiSearchQueryPlan
    evidence_summary: Optional[AiSearchEvidenceSummary] = None
    results: list[AiSearchResultItem] = Field(default_factory=list)
    topic_suggestions: list[AiSearchTopicSuggestion] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list)


class DouyinReportSection(StrictPayload):
    title: str = Field(min_length=1)
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class DouyinAccountReportOutput(StrictPayload):
    status: Literal["success", "partial_data", "insufficient_data"]
    account_id: str = ""
    account_name: str = ""
    platform: Literal["douyin"] = "douyin"
    analysis_scope: Literal["all_data", "month", "custom_range"]
    covered_period: dict[str, Any] | str = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    executive_summary: str
    key_metrics: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, Any] = Field(default_factory=dict)
    top_content_analysis: list[dict[str, Any]] = Field(default_factory=list)
    low_content_analysis: list[dict[str, Any]] = Field(default_factory=list)
    data_limitations: list[Any] | str = Field(default_factory=list)
    next_month_actions: list[str] = Field(default_factory=list)
    export_markdown: str = ""
    legacy_monthly_report: dict[str, Any] = Field(default_factory=dict)


class TableAuditItemOutput(StrictPayload):
    risk_level: Literal["low", "medium", "high", "unknown"]
    passed: bool
    issues: list[Any] = Field(default_factory=list)
    reason: str
    recommended_action: str = ""
    evidence: list[Any] = Field(default_factory=list)


class SmartFillEvidence(StrictPayload):
    file_id: str = Field(min_length=1)
    file_name: str = ""
    section: Optional[str] = None
    page: Optional[int] = Field(default=None, ge=1)
    paragraph_index: Optional[int] = Field(default=None, ge=0)
    table_index: Optional[int] = Field(default=None, ge=0)
    char_start: Optional[int] = Field(default=None, ge=0)
    char_end: Optional[int] = Field(default=None, ge=0)
    quote: str = Field(min_length=1)


class SmartFillFieldResult(StrictPayload):
    field_id: str = Field(min_length=1)
    status: Literal["filled", "missing", "conflict", "invalid_format", "needs_review"]
    value: Any = None
    evidence: list[SmartFillEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SmartFillGroupOutput(StrictPayload):
    group_id: SmartFillGroupId
    fields: list[SmartFillFieldResult] = Field(default_factory=list)
    missing_field_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


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
    "media_script_change_proposal_input": MediaScriptChangeProposalInput,
    "media_script_select_input": MediaScriptSelectInput,
    "media_chat_input": MediaChatInput,
    "media_script_main_agent_input": MediaScriptMainAgentInput,
    "ai_search_chat_input": AiSearchChatInput,
    "media_topic_search_input": MediaTopicSearchInput,
    "douyin_account_report_input": DouyinAccountReportInput,
    "table_audit_input": TableAuditInput,
    "smart_fill_extract_input": SmartFillExtractInput,
    "pipeline_demo_input": PipelineDemoInput,
}

_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "media_script_output": MediaScriptOutput,
    "media_script_workspace_output": MediaScriptWorkspaceOutput,
    "media_script_change_proposal_output": MediaScriptChangeProposalOutput,
    "ai_search_output": AiSearchOutput,
    "media_topic_search_output": AiSearchOutput,
    "douyin_account_report_output": DouyinAccountReportOutput,
    "table_audit_item_output": TableAuditItemOutput,
    "smart_fill_group_output": SmartFillGroupOutput,
    "batch_task_output": BatchTaskOutput,
    "pipeline_demo_analysis": PipelineDemoAnalysis,
    "pipeline_demo_normalized": PipelineDemoNormalized,
    "pipeline_demo_result": PipelineDemoResult,
    "media_script_context_bundle": MediaScriptContextBundle,
    "media_script_research_context": MediaScriptResearchContext,
    "media_script_research_bundle": MediaScriptResearchBundle,
    "media_script_writer_draft": MediaScriptWriterDraft,
    "media_storyboard_draft": MediaStoryboardDraft,
    "media_script_check_result": MediaScriptCheckResult,
    "media_script_review_result": MediaScriptReviewResult,
}


def register_input_schema(name: str, schema: type[BaseModel]) -> None:
    _register_schema(_INPUT_SCHEMAS, name, schema)


def register_output_schema(name: str, schema: type[BaseModel]) -> None:
    _register_schema(_OUTPUT_SCHEMAS, name, schema)


def _register_schema(registry: dict[str, type[BaseModel]], name: str, schema: type[BaseModel]) -> None:
    if not name:
        raise ValueError("Schema name is required.")
    existing = registry.get(name)
    if existing is not None and existing is not schema:
        raise ValueError(f"Schema '{name}' is already registered.")
    registry[name] = schema


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


def validate_stage_payload(schema_name: str | None, payload: Any) -> dict[str, Any]:
    if not schema_name:
        if not isinstance(payload, dict):
            raise ValueError("Stage payload must be a JSON object.")
        return payload
    schema = _OUTPUT_SCHEMAS.get(schema_name) or _INPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown stage schema '{schema_name}'.")
    try:
        return schema.model_validate(payload).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError(f"Stage payload does not match schema '{schema_name}': {exc.errors()}") from exc
