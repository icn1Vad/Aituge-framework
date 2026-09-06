from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from contract.api.models import (
    ContractProfile,
    ErrorData,
    Evidence,
    EvidenceType,
    Finding,
    LegalEvidenceVersionSnapshotReference,
    ReviewSummary,
    LegalEvidenceReference,
    StrictModel,
)
from contract.ir.models import IRDefinition, IRSemanticItem
from contract.evidence_planning.review_result import RuleReviewResult


StageId = Literal[
    "parse_contract",
    "resolve_parties",
    "extract_contract_ir",
    "rights_obligations_review",
    "commercial_terms_review",
    "liability_termination_review",
    "missing_ambiguous_clauses",
    "relation_extraction",
    "verify_evidence",
    "finalize_review",
]

STAGE_RESULT_TYPES = {
    "parse_contract": "PARSE_CONTRACT_STAGE_V1",
    "resolve_parties": "PARTY_RESOLUTION_STAGE_V1",
    "extract_contract_ir": "CONTRACT_IR_STAGE_V1",
    "rights_obligations_review": "RIGHTS_OBLIGATIONS_STAGE_V1",
    "commercial_terms_review": "COMMERCIAL_TERMS_STAGE_V1",
    "liability_termination_review": "LIABILITY_TERMINATION_STAGE_V1",
    "missing_ambiguous_clauses": "MISSING_AMBIGUITY_STAGE_V1",
    "relation_extraction": "RELATION_EXTRACTION_STAGE_V1",
    "verify_evidence": "EVIDENCE_VERIFICATION_STAGE_V1",
    "finalize_review": "FINAL_REVIEW_STAGE_V1",
}


class ParseContractStageResult(StrictModel):
    result_type: Literal["PARSE_CONTRACT_STAGE_V1"]
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    block_count: int = Field(gt=0)
    ir_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class StagePartyProfile(StrictModel):
    name: str = Field(min_length=1, max_length=500)
    name_resolved: bool = True
    name_status: Literal["EXTRACTED", "USER_CONFIRMED", "NOT_STATED"] = "EXTRACTED"


class PartyResolutionStageResult(StrictModel):
    result_type: Literal["PARTY_RESOLUTION_STAGE_V1"]
    resolution_status: Literal["RESOLVED", "PARTIAL"] = "RESOLVED"
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    party_a: StagePartyProfile | None = None
    party_b: StagePartyProfile | None = None
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str | None = Field(default=None, min_length=1, max_length=500)
    counterparty: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_perspective(self) -> "PartyResolutionStageResult":
        if self.resolution_status == "PARTIAL":
            if self.party_a is not None and self.party_b is not None:
                raise ValueError("PARTIAL party resolution cannot contain both parties")
            if self.our_party is not None or self.counterparty is not None:
                raise ValueError("PARTIAL party resolution cannot map review perspective")
            return self
        if self.party_a is None or self.party_b is None:
            raise ValueError("RESOLVED party resolution requires both parties")
        if self.our_party is None or self.counterparty is None:
            raise ValueError("RESOLVED party resolution requires perspective mapping")
        if self.party_a.name.casefold() == self.party_b.name.casefold():
            raise ValueError("resolved contract parties must be distinct")
        expected_our = self.party_a.name if self.perspective == "PARTY_A" else self.party_b.name
        expected_other = self.party_b.name if self.perspective == "PARTY_A" else self.party_a.name
        if self.our_party != expected_our or self.counterparty != expected_other:
            raise ValueError("Party mapping does not match perspective")
        return self


class ContractIrSemanticDelta(StrictModel):
    definitions: list[IRDefinition] = Field(default_factory=list)
    rights: list[IRSemanticItem] = Field(default_factory=list)
    obligations: list[IRSemanticItem] = Field(default_factory=list)
    prohibitions: list[IRSemanticItem] = Field(default_factory=list)
    payment_terms: list[IRSemanticItem] = Field(default_factory=list)
    delivery_terms: list[IRSemanticItem] = Field(default_factory=list)
    acceptance_terms: list[IRSemanticItem] = Field(default_factory=list)
    liabilities: list[IRSemanticItem] = Field(default_factory=list)
    termination_terms: list[IRSemanticItem] = Field(default_factory=list)
    confidentiality_terms: list[IRSemanticItem] = Field(default_factory=list)
    intellectual_property_terms: list[IRSemanticItem] = Field(default_factory=list)
    dispute_resolution: list[IRSemanticItem] = Field(default_factory=list)
    dates: list[IRSemanticItem] = Field(default_factory=list)
    amounts: list[IRSemanticItem] = Field(default_factory=list)


class ExtractContractIrStageResult(StrictModel):
    result_type: Literal["CONTRACT_IR_STAGE_V1"]
    semantic_ir: ContractIrSemanticDelta


class EvidenceCandidate(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    finding_id: str = Field(min_length=1, max_length=160)
    evidence_type: EvidenceType
    block_id: str | None = Field(default=None, min_length=1, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    quoted_text: str | None = None
    quoted_text_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    checked_scope: str | None = Field(default=None, min_length=1, max_length=500)
    verification_note: str | None = Field(default=None, min_length=1, max_length=5000)
    bounding_boxes: list[None] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def validate_candidate_shape(self) -> "EvidenceCandidate":
        positions = (self.block_id, self.char_start, self.char_end)
        if self.evidence_type in {EvidenceType.TEXT_QUOTE, EvidenceType.CONTEXT}:
            if any(value is None for value in positions):
                raise ValueError("Text evidence candidate requires block_id, char_start, and char_end")
            assert self.char_start is not None and self.char_end is not None
            if self.char_end <= self.char_start:
                raise ValueError("char_end must be greater than char_start")
            if (self.quoted_text is None) != (self.quoted_text_hash is None):
                raise ValueError("quoted_text and quoted_text_hash must be supplied together")
            if self.quoted_text is not None and self.quoted_text_hash is not None:
                if self.char_end - self.char_start != len(self.quoted_text):
                    raise ValueError("Candidate range length must match quoted_text")
                expected = "sha256:" + hashlib.sha256(self.quoted_text.encode("utf-8")).hexdigest()
                if self.quoted_text_hash != expected:
                    raise ValueError("quoted_text_hash does not match quoted_text")
        else:
            if self.checked_scope is None or self.verification_note is None:
                raise ValueError("ABSENCE evidence requires checked_scope and verification_note")
            if any(value is not None for value in positions):
                raise ValueError("ABSENCE evidence cannot contain text positioning fields")
            if self.page_number is not None or self.quoted_text is not None or self.quoted_text_hash is not None:
                raise ValueError("ABSENCE evidence cannot contain quoted text fields")
        return self


class ReviewStageResult(StrictModel):
    findings: list[Finding] = Field(default_factory=list)
    evidences: list[EvidenceCandidate] = Field(default_factory=list)


class FindingReference(StrictModel):
    artifact_type: str = Field(min_length=1, max_length=160)
    finding_id: str = Field(min_length=1, max_length=160)


class FindingConsolidationDecision(StrictModel):
    pair_id: str = Field(pattern=r"^pair-[0-9a-f]{32}$")
    left: FindingReference
    right: FindingReference
    relation: Literal["SAME_RISK", "RELATED_DISTINCT", "DISTINCT"]


class FindingConsolidationArtifact(StrictModel):
    result_type: Literal["FINDING_CONSOLIDATION_V1"]
    status: Literal["COMPLETED", "SKIPPED"]
    candidate_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    decisions: list[FindingConsolidationDecision] = Field(default_factory=list)
    skip_reason: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_result(self) -> "FindingConsolidationArtifact":
        if self.status == "COMPLETED":
            if self.skip_reason is not None or len(self.decisions) != self.candidate_count:
                raise ValueError("Completed consolidation must cover every candidate")
        elif self.decisions or self.skip_reason is None:
            raise ValueError("Skipped consolidation requires a reason and no decisions")
        pair_ids = [item.pair_id for item in self.decisions]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("Consolidation pair IDs must be unique")
        return self


class RightsObligationsStageResult(ReviewStageResult):
    result_type: Literal["RIGHTS_OBLIGATIONS_STAGE_V1"]


class CommercialTermsStageResult(ReviewStageResult):
    result_type: Literal["COMMERCIAL_TERMS_STAGE_V1"]


class LiabilityTerminationStageResult(ReviewStageResult):
    result_type: Literal["LIABILITY_TERMINATION_STAGE_V1"]


class MissingAmbiguityStageResult(ReviewStageResult):
    result_type: Literal["MISSING_AMBIGUITY_STAGE_V1"]


class InternalRelationship(StrictModel):
    source_clause_id: str = Field(min_length=1, max_length=160)
    target_clause_id: str = Field(min_length=1, max_length=160)
    relation_type: Literal["SUPPORTS", "CONFLICTS", "DEPENDS_ON", "OVERRIDES"]
    explanation: str = Field(min_length=1, max_length=4000)


class RelationExtractionStageResult(ReviewStageResult):
    result_type: Literal["RELATION_EXTRACTION_STAGE_V1"]
    internal_relationships: list[InternalRelationship] = Field(default_factory=list)


class EvidenceVerificationStageResult(StrictModel):
    result_type: Literal["EVIDENCE_VERIFICATION_STAGE_V1"]
    contract_profile: ContractProfile
    overview: str = Field(min_length=1, max_length=10_000)
    findings: list[Finding] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    relationships: list[None] = Field(default_factory=list, max_length=0)


class FinalizeReviewStageResult(StrictModel):
    rule_review: "RuleReviewResult | None" = None
    result_type: Literal["FINAL_REVIEW_STAGE_V1"]
    schema_version: Literal["1.0"]
    review_id: str = Field(min_length=1, max_length=160)
    business_task_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    contract_profile: ContractProfile
    summary: ReviewSummary
    findings: list[Finding]
    evidences: list[Evidence]
    legal_evidence_release_id: str | None = None
    legal_evidence_bundle_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    legal_evidence_version_snapshot: LegalEvidenceVersionSnapshotReference | None = None
    legal_evidences: list[LegalEvidenceReference] = Field(default_factory=list)
    relationships: list[None] = Field(default_factory=list, max_length=0)


StageResult = Annotated[
    ParseContractStageResult
    | PartyResolutionStageResult
    | ExtractContractIrStageResult
    | RightsObligationsStageResult
    | CommercialTermsStageResult
    | LiabilityTerminationStageResult
    | MissingAmbiguityStageResult
    | RelationExtractionStageResult
    | EvidenceVerificationStageResult
    | FinalizeReviewStageResult,
    Field(discriminator="result_type"),
]


class CallbackBase(StrictModel):
    schema_version: Literal["1.0"]
    review_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    framework_task_id: str = Field(min_length=1, max_length=160)
    framework_run_id: str = Field(min_length=1, max_length=160)
    lease_version: int = Field(default=1, ge=1)
    event_sequence: int = Field(ge=0)
    callback_id: str = Field(min_length=1, max_length=200)


class StageResultCallback(CallbackBase):
    callback_type: Literal["STAGE_RESULT"]
    stage_id: StageId
    result: StageResult
    error: None

    @model_validator(mode="after")
    def validate_stage_result_type(self) -> "StageResultCallback":
        expected = STAGE_RESULT_TYPES[self.stage_id]
        if self.result.result_type != expected:
            raise ValueError(f"{self.stage_id} requires result_type={expected}")
        return self


class RunSucceededCallback(CallbackBase):
    callback_type: Literal["RUN_SUCCEEDED"]
    stage_id: None
    result: None
    error: None


class RunFailedCallback(CallbackBase):
    callback_type: Literal["RUN_FAILED"]
    stage_id: StageId | None
    result: None
    error: ErrorData


FrameworkCallback = Annotated[
    StageResultCallback | RunSucceededCallback | RunFailedCallback,
    Field(discriminator="callback_type"),
]


class FrameworkCallbackData(StrictModel):
    accepted: bool
    duplicate: bool
    ignored_reason: str | None = None


class FrameworkTaskInput(StrictModel):
    schema_version: Literal["1.0"]
    review_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    business_task_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    party_resolution_id: str | None = Field(default=None, min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party_name: str | None = Field(default=None, max_length=500)
    execution_mode: Literal["FULL_REVIEW", "PARTY_RESOLUTION"] = "FULL_REVIEW"
    confirmed_party_a_name: str | None = Field(default=None, min_length=1, max_length=500)
    confirmed_party_b_name: str | None = Field(default=None, min_length=1, max_length=500)
    contract_type: Literal["AUTO"]
    review_attitude: Literal["NEUTRAL"]
    rule_review_standard: Literal["neutral", "strong", "weak"] = "neutral"

    @model_validator(mode="after")
    def validate_confirmed_parties(self) -> "FrameworkTaskInput":
        if (self.confirmed_party_a_name is None) != (self.confirmed_party_b_name is None):
            raise ValueError("confirmed contract parties must be supplied together")
        if (
            self.confirmed_party_a_name is not None
            and self.confirmed_party_a_name == self.confirmed_party_b_name
        ):
            raise ValueError("confirmed contract parties must be distinct")
        return self


class StageExecuteRequest(StrictModel):
    schema_version: Literal["1.0"]
    review_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    framework_task_id: str = Field(min_length=1, max_length=160)
    framework_run_id: str = Field(min_length=1, max_length=160)
    stage_id: Literal["parse_contract", "verify_evidence", "finalize_review"]
    task_input: FrameworkTaskInput
    artifacts: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity(self) -> "StageExecuteRequest":
        if (
            self.review_id != self.task_input.review_id
            or self.attempt_no != self.task_input.attempt_no
        ):
            raise ValueError("Stage request identity does not match task_input")
        return self


GatewayStageResult = Annotated[
    ParseContractStageResult | EvidenceVerificationStageResult | FinalizeReviewStageResult,
    Field(discriminator="result_type"),
]
