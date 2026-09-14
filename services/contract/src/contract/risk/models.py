from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from contract.api.models import FindingCategory, Perspective, StrictModel
from contract.callback.models import ExtractContractIrStageResult
from contract.ir.models import SourceAnchor
from contract.risk.review_ledger import CheckTaskScope


RiskDomain = Literal[
    "formation_validity_authority",
    "commercial_financial",
    "performance_obligations",
    "ip_confidentiality_data",
    "liability_remedies_exit",
    "cross_clause_consistency",
    "missing_ambiguity_completeness",
]
IrField = Literal[
    "definitions",
    "rights",
    "obligations",
    "prohibitions",
    "payment_terms",
    "delivery_terms",
    "acceptance_terms",
    "liabilities",
    "termination_terms",
    "confidentiality_terms",
    "intellectual_property_terms",
    "dispute_resolution",
    "dates",
    "amounts",
]
LegacyArtifactType = Literal[
    "rights_obligations_review_result",
    "commercial_terms_review_result",
    "liability_termination_review_result",
    "missing_ambiguous_clauses_result",
    "relation_extraction_result",
]


class Criticality(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    ADVISORY = "ADVISORY"


class ExecutionMode(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    EXTEND_DOMAIN = "EXTEND_DOMAIN"
    SPECIALIST_REVIEWER = "SPECIALIST_REVIEWER"


class ReviewUnitType(str, Enum):
    BASE = "BASE"
    HORIZONTAL = "HORIZONTAL"
    SPECIALIST = "SPECIALIST"


class ApplicabilitySpec(StrictModel):
    contract_types: list[str] = Field(default_factory=lambda: ["AUTO"], min_length=1)
    perspectives: list[Perspective] = Field(
        default_factory=lambda: [Perspective.PARTY_A, Perspective.PARTY_B],
        min_length=1,
    )
    review_attitudes: list[Literal["NEUTRAL"]] = Field(default_factory=lambda: ["NEUTRAL"])

    def matches(self, *, contract_type: str, perspective: Perspective, review_attitude: str) -> bool:
        return (
            ("*" in self.contract_types or contract_type in self.contract_types)
            and perspective in self.perspectives
            and review_attitude in self.review_attitudes
        )


class EvidenceRequirement(StrictModel):
    allowed_types: list[Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]] = Field(
        default_factory=lambda: ["TEXT_QUOTE", "CONTEXT", "ABSENCE"],
        min_length=1,
    )
    minimum_per_finding: int = Field(default=1, ge=1, le=10)
    require_source_anchor: bool = True


class SpecialistReviewerSpec(StrictModel):
    specialist_id: str = Field(min_length=1, max_length=160)
    trigger_check_codes: list[str] = Field(min_length=1)
    maximum_invocations: Literal[1] = 1


class CheckSpec(StrictModel):
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    domain: RiskDomain
    criticality: Criticality = Criticality.REQUIRED
    applicability: ApplicabilitySpec = Field(default_factory=ApplicabilitySpec)
    required_ir_types: list[IrField] = Field(default_factory=list)
    review_question: str = Field(min_length=1)
    allowed_categories: list[FindingCategory] = Field(min_length=1)
    allowed_risk_types: list[str] = Field(min_length=1)
    risk_level_rule_id: str = Field(min_length=1, max_length=160)
    evidence_requirement: EvidenceRequirement = Field(default_factory=EvidenceRequirement)
    deterministic_validator_ids: list[str] = Field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    priority: int = Field(default=100, ge=1, le=10_000)
    enabled: bool = True
    legacy_artifact_type: LegacyArtifactType

    @model_validator(mode="after")
    def validate_unique_values(self) -> "CheckSpec":
        for name, values in (
            ("required_ir_types", self.required_ir_types),
            ("allowed_categories", self.allowed_categories),
            ("allowed_risk_types", self.allowed_risk_types),
            ("deterministic_validator_ids", self.deterministic_validator_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique values")
        return self


class PlaybookManifest(StrictModel):
    playbook_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled: bool = True
    execution_mode: ExecutionMode
    criticality: Criticality = Criticality.REQUIRED
    applicability: ApplicabilitySpec = Field(default_factory=ApplicabilitySpec)
    required_ir_types: list[IrField] = Field(default_factory=list)
    target_domains: list[RiskDomain] = Field(min_length=1)
    check_codes: list[str] = Field(min_length=1)
    risk_level_rule_ids: list[str] = Field(min_length=1)
    evidence_policy_id: str = Field(min_length=1, max_length=160)
    deterministic_validator_ids: list[str] = Field(default_factory=list)
    specialist_reviewer_spec: SpecialistReviewerSpec | None = None
    test_case_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode(self) -> "PlaybookManifest":
        if len(self.target_domains) != len(set(self.target_domains)):
            raise ValueError("target_domains must be unique")
        if len(self.check_codes) != len(set(self.check_codes)):
            raise ValueError("check_codes must be unique")
        if self.execution_mode == ExecutionMode.SPECIALIST_REVIEWER:
            if self.specialist_reviewer_spec is None:
                raise ValueError("SPECIALIST_REVIEWER requires specialist_reviewer_spec")
        elif self.specialist_reviewer_spec is not None:
            raise ValueError("Only SPECIALIST_REVIEWER can define specialist_reviewer_spec")
        return self


class LegacyFindingMapping(StrictModel):
    domain: RiskDomain
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    category: FindingCategory
    legacy_artifact_type: LegacyArtifactType


class RiskSourceBlock(StrictModel):
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    text: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)


class RiskProjectedIrItem(StrictModel):
    ir_type: IrField
    item_id: str = Field(min_length=1, max_length=160)
    subject: str | None = None
    predicate: str = Field(min_length=1)
    object: str | None = None
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class RiskSourceExcerpt(StrictModel):
    anchor_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    heading_path: list[str] = Field(default_factory=list)


class RiskEvidenceSource(StrictModel):
    source_id: str = Field(pattern=r"^risk-es-[0-9a-f]{32}$")
    generation_id: str = Field(min_length=1, max_length=160)
    ir_item_id: str = Field(min_length=1, max_length=160)
    anchor_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT"]
    ir_type: IrField
    subject: str | None = None
    predicate: str = Field(min_length=1)
    object: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    allowed_check_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_literal_source(self) -> "RiskEvidenceSource":
        if self.char_end - self.char_start != len(self.quoted_text):
            raise ValueError("Evidence Source offsets must match quoted_text length")
        expected = "sha256:" + hashlib.sha256(
            self.quoted_text.encode("utf-8")
        ).hexdigest()
        if self.quoted_text_hash != expected:
            raise ValueError("Evidence Source quoted_text_hash is invalid")
        if len(self.allowed_check_codes) != len(set(self.allowed_check_codes)):
            raise ValueError("Evidence Source allowed_check_codes must be unique")
        return self


class RiskAbsenceEvidenceSource(StrictModel):
    source_id: str = Field(pattern=r"^risk-as-[0-9a-f]{32}$")
    generation_id: str = Field(min_length=1, max_length=160)
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    checked_scope: str = Field(min_length=1)
    verification_method: str = Field(min_length=1)
    present_ir_types: list[IrField]
    missing_target: str = Field(min_length=1)


class RiskCheckEvidencePolicy(StrictModel):
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    allowed_evidence_source_ids: list[str] = Field(default_factory=list)
    allowed_absence_source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_sources(self) -> "RiskCheckEvidencePolicy":
        if len(self.allowed_evidence_source_ids) != len(
            set(self.allowed_evidence_source_ids)
        ):
            raise ValueError("allowed_evidence_source_ids must be unique")
        if len(self.allowed_absence_source_ids) != len(
            set(self.allowed_absence_source_ids)
        ):
            raise ValueError("allowed_absence_source_ids must be unique")
        return self


class RiskClauseCatalogItem(StrictModel):
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    page_number: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    anchor_ids: list[str] = Field(min_length=1)


class RiskHorizontalCandidate(StrictModel):
    candidate_id: str = Field(pattern=r"^candidate-[0-9a-f]{32}$")
    candidate_type: Literal[
        "VALUE_CONFLICT",
        "OBLIGATION_CONFLICT",
        "BROKEN_REFERENCE",
        "MISSING_REQUIRED_IR",
        "AMBIGUOUS_TEXT",
    ]
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    item_ids: list[str] = Field(default_factory=list)
    anchor_ids: list[str] = Field(default_factory=list)
    reason_code: str = Field(min_length=1, max_length=160)


class RiskCoverageSummary(StrictModel):
    available_ir_types: list[IrField]
    missing_ir_types: list[IrField]
    total_ir_item_count: int = Field(ge=0)
    projected_ir_item_count: int = Field(ge=0)
    source_block_count: int = Field(ge=0)
    source_excerpt_count: int = Field(ge=0)


class ReviewBatchSpec(StrictModel):
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    check_codes: list[str] = Field(min_length=1)
    required_ir_types: list[IrField]
    projected_item_ids: list[str]
    source_anchor_ids: list[str]
    estimated_input_tokens: int = Field(
        ge=0,
        description=(
            "Compatibility field: deterministic Business Context size estimate, "
            "not a provider prompt-token measurement."
        ),
    )

    @property
    def estimated_business_context_tokens(self) -> int:
        return self.estimated_input_tokens


class ReviewUnitSpec(StrictModel):
    unit_id: RiskDomain
    unit_type: ReviewUnitType
    domain: RiskDomain
    required: bool
    check_specs: list[CheckSpec] = Field(min_length=1)
    required_ir_types: list[IrField]
    ir_projection_fields: list[IrField]
    source_selection_policy: Literal["CHECK_IR_TYPES_AND_ANCHORS"]
    evidence_policy_id: str = Field(min_length=1, max_length=160)
    deterministic_validator_ids: list[str]
    model_id: str = Field(min_length=1, max_length=160)
    output_schema_version: Literal["1.0"] = "1.0"
    temperature: Literal[0] = 0
    thinking_enabled: Literal[False] = False
    max_repairs: Literal[1] = 1
    timeout_seconds: int = Field(gt=0, le=300)
    target_input_tokens_min: int = Field(ge=0)
    target_input_tokens_max: int = Field(gt=0)
    soft_input_token_limit: int = Field(gt=0)
    hard_input_token_limit: int = Field(gt=0)
    target_output_token_limit: int = Field(gt=0)
    soft_output_token_limit: int = Field(gt=0)
    hard_output_token_limit: int = Field(gt=0)
    requires_model: bool
    batch_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_limits(self) -> "ReviewUnitSpec":
        if not (
            self.target_input_tokens_min
            <= self.target_input_tokens_max
            <= self.soft_input_token_limit
            <= self.hard_input_token_limit
        ):
            raise ValueError("input token limits are not monotonic")
        if not (
            self.target_output_token_limit
            <= self.soft_output_token_limit
            <= self.hard_output_token_limit
        ):
            raise ValueError("output token limits are not monotonic")
        if self.requires_model != bool(self.batch_ids):
            raise ValueError("requires_model must match whether the unit has executable batches")
        return self


class RiskReviewContext(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    plan_id: str = Field(min_length=1, max_length=160)
    unit_id: RiskDomain
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    perspective: Perspective
    our_party: str = Field(min_length=1)
    counterparty: str = Field(min_length=1)
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"
    check_specs: list[CheckSpec] = Field(min_length=1)
    check_task_scopes: list[CheckTaskScope] = Field(default_factory=list)
    definitions: list[RiskProjectedIrItem] = Field(default_factory=list)
    projected_ir_items: list[RiskProjectedIrItem] = Field(default_factory=list)
    clause_catalog: list[RiskClauseCatalogItem] = Field(default_factory=list)
    source_excerpts: list[RiskSourceExcerpt] = Field(default_factory=list)
    source_anchor_index: list[RiskSourceExcerpt] = Field(default_factory=list)
    evidence_sources: list[RiskEvidenceSource] = Field(default_factory=list)
    absence_evidence_sources: list[RiskAbsenceEvidenceSource] = Field(
        default_factory=list
    )
    check_evidence_policies: list[RiskCheckEvidencePolicy] = Field(
        default_factory=list
    )
    present_ir_types: list[IrField]
    missing_ir_types: list[IrField]
    coverage_summary: RiskCoverageSummary
    horizontal_candidates: list[RiskHorizontalCandidate] = Field(default_factory=list)
    estimated_input_tokens: int = Field(
        ge=0,
        description=(
            "Compatibility field: deterministic Business Context size estimate, "
            "not a provider prompt-token measurement."
        ),
    )
    context_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def estimated_business_context_tokens(self) -> int:
        return self.estimated_input_tokens


class DeterministicCheckResult(StrictModel):
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    status: Literal["REVIEWED", "NOT_APPLICABLE"]
    reason_code: str = Field(min_length=1, max_length=160)


class RiskReviewPlanInput(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    perspective: Perspective
    our_party: str = Field(min_length=1)
    counterparty: str = Field(min_length=1)
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"
    stage_result: ExtractContractIrStageResult
    source_blocks: list[RiskSourceBlock] = Field(min_length=1)
    selected_playbook_ids: list[str] = Field(default_factory=lambda: ["base_neutral"])
    horizontal_candidates: list[RiskHorizontalCandidate] = Field(default_factory=list)


class RiskReviewPlan(StrictModel):
    plan_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(pattern=r"^risk-plan-[0-9a-f]{32}$")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    perspective: Perspective
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"
    selected_playbook_ids: list[str] = Field(min_length=1)
    review_units: list[ReviewUnitSpec] = Field(min_length=7)
    contexts: list[RiskReviewContext] = Field(default_factory=list)
    deterministic_checks: list[DeterministicCheckResult] = Field(default_factory=list)
    specialist_reviewer_count: int = Field(ge=0, le=2)
    max_concurrency: int = Field(default=7, ge=1, le=7)
    required_unit_ids: list[RiskDomain] = Field(min_length=5)

    @model_validator(mode="after")
    def validate_plan(self) -> "RiskReviewPlan":
        unit_ids = [unit.unit_id for unit in self.review_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("review unit IDs must be unique")
        if not set(self.required_unit_ids).issubset(unit_ids):
            raise ValueError("required_unit_ids must refer to review units")
        context_ids = [context.batch_id for context in self.contexts]
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("context batch IDs must be unique")
        if {batch_id for unit in self.review_units for batch_id in unit.batch_ids} != set(context_ids):
            raise ValueError("every executable batch must have exactly one context")
        return self
