from __future__ import annotations

from datetime import date
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


def _iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format.") from exc


def _normalized_text(value: str) -> str:
    return "".join(value.split()).casefold()


class IndustryCalendarLookupPhase(StrictPayload):
    phase_key: str = Field(min_length=1, max_length=120)
    phase_name: str = Field(min_length=1, max_length=160)
    evidence_keywords: list[str] = Field(min_length=1, max_length=12)
    known_source_url: str = Field(default="", max_length=1000)
    forecast_date: Optional[str] = None

    @model_validator(mode="after")
    def normalize_phase(self):
        self.phase_key = self.phase_key.strip()
        self.phase_name = self.phase_name.strip()
        self.evidence_keywords = [item.strip() for item in self.evidence_keywords if item.strip()]
        if not self.evidence_keywords:
            raise ValueError("Phase evidence_keywords must not be empty.")
        if self.forecast_date:
            _iso_date(self.forecast_date, "forecast_date")
        return self


class IndustryCalendarOfficialDateLookupInput(TaskPayloadBase):
    template_id: str = Field(min_length=1, max_length=120)
    event_name: str = Field(min_length=1, max_length=160)
    cycle_year: int = Field(ge=2000, le=2100)
    lookup_number: int = Field(ge=0, le=3)
    lookup_type: Literal["external_search", "official_page_check", "bootstrap_search"]
    official_domains: list[str] = Field(min_length=1, max_length=12)
    preferred_official_domains: list[str] = Field(default_factory=list, max_length=6)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    phases: list[IndustryCalendarLookupPhase] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def normalize_lookup_scope(self):
        self.template_id = self.template_id.strip()
        self.event_name = self.event_name.strip()
        self.official_domains = [item.strip().lower().rstrip(".") for item in self.official_domains if item.strip()]
        self.preferred_official_domains = [
            item.strip().lower().rstrip(".") for item in self.preferred_official_domains if item.strip()
        ]
        self.keywords = [item.strip() for item in self.keywords if item.strip()]
        if not self.official_domains:
            raise ValueError("At least one official domain is required.")
        if any(item not in self.official_domains for item in self.preferred_official_domains):
            raise ValueError("preferred_official_domains must be a subset of official_domains.")
        phase_keys = [item.phase_key for item in self.phases]
        if len(phase_keys) != len(set(phase_keys)):
            raise ValueError("Phase keys must be unique.")
        return self


class HistoryViralMetricPercentiles(StrictPayload):
    play_count: Optional[float] = Field(default=None, ge=0, le=1)
    like_count: Optional[float] = Field(default=None, ge=0, le=1)
    comment_count: Optional[float] = Field(default=None, ge=0, le=1)
    share_count: Optional[float] = Field(default=None, ge=0, le=1)
    collect_count: Optional[float] = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def require_available_metric(self):
        values = (self.play_count, self.like_count, self.comment_count, self.share_count, self.collect_count)
        if all(value is None for value in values):
            raise ValueError("At least one metric percentile is required.")
        return self


class HistoryViralSourceItem(StrictPayload):
    id: str = Field(default="", min_length=1, max_length=160)
    original_content_id: str = Field(min_length=1, max_length=160)
    original_title: str = Field(min_length=1, max_length=300)
    original_publish_time: str = Field(min_length=1, max_length=80)
    source_url: str = Field(default="", max_length=1000)
    account_percentile_score: float = Field(ge=0, le=1)
    metric_percentiles: HistoryViralMetricPercentiles

    @model_validator(mode="before")
    @classmethod
    def default_item_id_to_content_id(cls, value):
        if isinstance(value, dict) and not str(value.get("id") or "").strip():
            value = {**value, "id": value.get("original_content_id")}
        return value

    @model_validator(mode="after")
    def require_stable_content_identity(self):
        self.id = self.id.strip()
        self.original_content_id = self.original_content_id.strip()
        self.original_title = self.original_title.strip()
        self.original_publish_time = self.original_publish_time.strip()
        self.source_url = self.source_url.strip()
        if self.id != self.original_content_id:
            raise ValueError("id must equal original_content_id for stable TaskItem identity.")
        if not self.original_title or not self.original_publish_time:
            raise ValueError("original_title and original_publish_time must not be blank.")
        return self


class HistoryViralTopicVariantsInput(TaskPayloadBase):
    account_id: str = Field(min_length=1, max_length=160)
    business_date: str = Field(min_length=10, max_length=10)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    content_snapshot: str = Field(min_length=8, max_length=160)
    variants_per_source: int = Field(default=3, ge=1, le=3)
    items: list[HistoryViralSourceItem] = Field(min_length=1, max_length=10)
    max_concurrency: int = Field(default=4, ge=1, le=4)
    failure_policy: Literal["continue"] = "continue"
    retry_per_item: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_business_context(self):
        _iso_date(self.business_date, "business_date")
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("History viral source item ids must be unique.")
        return self


class CalendarDateRange(StrictPayload):
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_range(self):
        start = _iso_date(self.start, "date_range.start")
        end = _iso_date(self.end, "date_range.end")
        if end < start:
            raise ValueError("date_range.end must not be before date_range.start.")
        return self


class IndustryCalendarSourceItem(StrictPayload):
    id: str = Field(default="", min_length=1, max_length=160)
    event_id: str = Field(min_length=1, max_length=160)
    event_name: str = Field(min_length=1, max_length=300)
    cycle_year: int = Field(ge=2000, le=2100)
    phase_key: str = Field(min_length=1, max_length=100)
    trigger_phase: str = Field(min_length=1, max_length=100)
    event_status: Literal["fixed", "confirmed"]
    event_date: Optional[str] = Field(default=None, min_length=10, max_length=10)
    date_range: Optional[CalendarDateRange] = None
    days_until_event: int = Field(ge=-31, le=366)
    source_url: str = Field(default="", max_length=1000)
    official_published_at: Optional[str] = Field(default=None, max_length=80)
    official_source_title: str = Field(default="", max_length=300)
    valid_from: str = Field(default="", max_length=10)
    valid_until: str = Field(default="", max_length=10)
    keywords: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="before")
    @classmethod
    def default_item_id_to_event_id(cls, value):
        if isinstance(value, dict) and not str(value.get("id") or "").strip():
            value = {**value, "id": value.get("event_id")}
        return value

    @model_validator(mode="after")
    def validate_event_facts(self):
        self.id = self.id.strip()
        self.event_id = self.event_id.strip()
        self.event_name = self.event_name.strip()
        self.phase_key = self.phase_key.strip()
        self.trigger_phase = self.trigger_phase.strip()
        self.source_url = self.source_url.strip()
        self.official_source_title = self.official_source_title.strip()
        if self.id != self.event_id:
            raise ValueError("id must equal event_id for stable TaskItem identity.")
        if not self.event_name or not self.phase_key or not self.trigger_phase:
            raise ValueError("event_name, phase_key, and trigger_phase must not be blank.")
        if (self.event_date is None) == (self.date_range is None):
            raise ValueError("Exactly one of event_date or date_range is required.")
        anchor = _iso_date(self.event_date or self.date_range.start, "event_date")  # type: ignore[union-attr]
        if anchor.year != self.cycle_year:
            raise ValueError("cycle_year must match the event date year.")
        if bool(self.valid_from) != bool(self.valid_until):
            raise ValueError("valid_from and valid_until must be provided together.")
        if self.valid_from:
            valid_from = _iso_date(self.valid_from, "valid_from")
            valid_until = _iso_date(self.valid_until, "valid_until")
            if valid_until < valid_from:
                raise ValueError("valid_until must not be before valid_from.")
        if self.event_status == "confirmed" and not self.source_url:
            raise ValueError("Confirmed events require source_url.")
        return self


class IndustryCalendarTopicCopyInput(TaskPayloadBase):
    business_date: str = Field(min_length=10, max_length=10)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    calendar_revision: int = Field(ge=0)
    config_version: str = Field(default="", max_length=80)
    items: list[IndustryCalendarSourceItem] = Field(min_length=1, max_length=20)
    max_concurrency: int = Field(default=4, ge=1, le=4)
    failure_policy: Literal["continue"] = "continue"
    retry_per_item: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_calendar_context(self):
        business_date = _iso_date(self.business_date, "business_date")
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Industry calendar item ids must be unique.")
        for item in self.items:
            anchor = _iso_date(item.event_date or item.date_range.start, "event_date")  # type: ignore[union-attr]
            if (anchor - business_date).days != item.days_until_event:
                raise ValueError(f"days_until_event does not match business_date for event '{item.event_id}'.")
            if item.valid_from:
                valid_from = _iso_date(item.valid_from, "valid_from")
                valid_until = _iso_date(item.valid_until, "valid_until")
                if not valid_from <= business_date <= valid_until:
                    raise ValueError(f"business_date is outside the validity window for event '{item.event_id}'.")
        return self


class MediaTopicSearchInput(TaskPayloadBase):
    message: Optional[str] = None
    topic_query: Optional[str] = None
    search_goal: str = ""
    search_mode: Literal["specific_search", "hotspot_discovery", "general_search_chat"] = "specific_search"
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


class DouyinContentAnalysisItem(StrictPayload):
    id: str = Field(min_length=1, max_length=160)
    content_id: str = Field(min_length=1, max_length=160)
    rank_type: Literal["best", "worst"]
    rank: int = Field(ge=1, le=3)
    evidence: dict[str, Any]


class DouyinContentAnalysisBatchInput(TaskPayloadBase):
    account_id: str = Field(min_length=1)
    period: dict[str, Any]
    failure_policy: Literal["continue"] = "continue"
    retry_per_item: int = Field(default=0, ge=0, le=3)
    items: list[DouyinContentAnalysisItem] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_unique_items(self):
        ids = [item.id for item in self.items]
        content_ids = [item.content_id for item in self.items]
        if len(ids) != len(set(ids)) or len(content_ids) != len(set(content_ids)):
            raise ValueError("Content analysis item ids and content_id values must be unique.")
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


class IndustryCalendarPhaseLookupResult(StrictPayload):
    phase_key: str = Field(min_length=1, max_length=120)
    status: Literal["confirmed", "candidate", "awaiting_official"]
    found: bool = False
    official: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    deadline_at: Optional[str] = None
    source_url: str = ""
    source_title: str = ""
    source_published_at: Optional[str] = None
    extraction_method: Literal["official_search", "official_page_check"] = "official_search"
    evidence_summary: str = ""
    evidence_excerpt: str = Field(default="", max_length=1600)
    warnings: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_confirmed_evidence(self):
        self.phase_key = self.phase_key.strip()
        if self.status == "confirmed":
            if not self.found or not self.official:
                raise ValueError("Confirmed lookup output must be found and official.")
            if not self.start_date or not self.source_url or not self.source_title or not self.evidence_excerpt:
                raise ValueError("Confirmed lookup output requires date, source URL, source title, and evidence excerpt.")
            start = _iso_date(self.start_date, "start_date")
            end = _iso_date(self.end_date or self.start_date, "end_date")
            if end < start:
                raise ValueError("end_date must not be before start_date.")
            self.end_date = end.isoformat()
        elif self.found and not self.source_url:
            raise ValueError("Found evidence requires source_url.")
        return self


class IndustryCalendarOfficialDateLookupOutput(StrictPayload):
    status: Literal["completed", "partial", "not_found", "search_failed"]
    phase_results: list[IndustryCalendarPhaseLookupResult] = Field(min_length=1, max_length=12)
    source_urls: list[str] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_unique_phase_results(self):
        phase_keys = [item.phase_key for item in self.phase_results]
        if len(phase_keys) != len(set(phase_keys)):
            raise ValueError("phase_results must contain unique phase_key values.")
        return self


class HistoryViralTopicVariant(StrictPayload):
    variant_index: int = Field(ge=1, le=3)
    generated_topic: str = Field(min_length=1, max_length=240)
    variant_angle: str = Field(min_length=1, max_length=120)
    recommendation_reason: str = Field(min_length=1, max_length=320)
    relation_to_source: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def reject_blank_creative_fields(self):
        fields = (self.generated_topic, self.variant_angle, self.recommendation_reason, self.relation_to_source)
        if not all(value.strip() for value in fields):
            raise ValueError("History variant creative fields must not be blank.")
        return self


class HistoryViralTopicVariantItemOutput(StrictPayload):
    source_item_id: str = Field(min_length=1, max_length=160)
    status: Literal["generated", "insufficient_context"]
    variants: list[HistoryViralTopicVariant] = Field(default_factory=list, max_length=3)
    warnings: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_variant_set(self):
        if self.status == "generated" and not self.variants:
            raise ValueError("Generated history output must contain at least one variant.")
        if self.status == "insufficient_context" and self.variants:
            raise ValueError("Insufficient-context history output must not contain variants.")
        if self.status == "insufficient_context" and not self.warnings:
            raise ValueError("Insufficient-context history output must explain the missing context.")
        indexes = [item.variant_index for item in self.variants]
        if indexes != list(range(1, len(indexes) + 1)):
            raise ValueError("variant_index values must be consecutive and start at 1.")
        topics = [_normalized_text(item.generated_topic) for item in self.variants]
        angles = [_normalized_text(item.variant_angle) for item in self.variants]
        if len(topics) != len(set(topics)):
            raise ValueError("generated_topic values must be distinct within one source item.")
        if len(angles) != len(set(angles)):
            raise ValueError("variant_angle values must be distinct within one source item.")
        return self


class IndustryCalendarTopicCopyItemOutput(StrictPayload):
    source_item_id: str = Field(min_length=1, max_length=160)
    status: Literal["generated", "insufficient_context"]
    generated_topic: str = Field(default="", max_length=240)
    variant_angle: str = Field(default="", max_length=120)
    recommendation_reason: str = Field(default="", max_length=320)
    warnings: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_generated_copy(self):
        creative_fields = (
            self.generated_topic.strip(),
            self.variant_angle.strip(),
            self.recommendation_reason.strip(),
        )
        if self.status == "generated" and not all(creative_fields):
            raise ValueError("Generated calendar output requires generated_topic, variant_angle, and recommendation_reason.")
        if self.status == "insufficient_context" and any(creative_fields):
            raise ValueError("Insufficient-context calendar output must not contain generated copy.")
        if self.status == "insufficient_context" and not self.warnings:
            raise ValueError("Insufficient-context calendar output must explain the missing context.")
        return self


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


class DouyinContentAnalysisItemOutput(StrictPayload):
    content_id: str = Field(min_length=1)
    evidence_based_reason: list[str] = Field(min_length=1)
    copy_and_angle_analysis: list[str] = Field(default_factory=list)
    comment_feedback_analysis: list[str] = Field(default_factory=list)
    trend_analysis: list[str] = Field(default_factory=list)
    reusable_lessons: list[str] = Field(default_factory=list)
    improvement_suggestions: list[str] = Field(default_factory=list)
    data_limitations: list[str] = Field(default_factory=list)


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
    "industry_calendar_official_date_lookup_input": IndustryCalendarOfficialDateLookupInput,
    "media_topic_search_input": MediaTopicSearchInput,
    "history_viral_topic_variants_input": HistoryViralTopicVariantsInput,
    "industry_calendar_topic_copy_input": IndustryCalendarTopicCopyInput,
    "douyin_account_report_input": DouyinAccountReportInput,
    "douyin_content_analysis_batch_input": DouyinContentAnalysisBatchInput,
    "table_audit_input": TableAuditInput,
    "pipeline_demo_input": PipelineDemoInput,
}

_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "media_script_output": MediaScriptOutput,
    "ai_search_output": AiSearchOutput,
    "industry_calendar_official_date_lookup_output": IndustryCalendarOfficialDateLookupOutput,
    "media_topic_search_output": AiSearchOutput,
    "history_viral_topic_variant_item_output": HistoryViralTopicVariantItemOutput,
    "industry_calendar_topic_copy_item_output": IndustryCalendarTopicCopyItemOutput,
    "douyin_account_report_output": DouyinAccountReportOutput,
    "douyin_content_analysis_item_output": DouyinContentAnalysisItemOutput,
    "table_audit_item_output": TableAuditItemOutput,
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
