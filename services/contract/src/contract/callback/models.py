from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from contract.api.models import (
    ContractProfile,
    ErrorData,
    Evidence,
    Finding,
    PartyProfile,
    ReviewSummary,
    StrictModel,
)
from contract.ir.models import ContractIR


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


class PartyResolutionStageResult(StrictModel):
    result_type: Literal["PARTY_RESOLUTION_STAGE_V1"]
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    party_a: PartyProfile
    party_b: PartyProfile
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_perspective(self) -> "PartyResolutionStageResult":
        expected_our = self.party_a.name if self.perspective == "PARTY_A" else self.party_b.name
        expected_other = self.party_b.name if self.perspective == "PARTY_A" else self.party_a.name
        if self.our_party != expected_our or self.counterparty != expected_other:
            raise ValueError("Party mapping does not match perspective")
        return self


class ExtractContractIrStageResult(StrictModel):
    result_type: Literal["CONTRACT_IR_STAGE_V1"]
    contract_ir: ContractIR

    @model_validator(mode="after")
    def validate_resolved_parties(self) -> "ExtractContractIrStageResult":
        parties = {party.role: party.name for party in self.contract_ir.parties}
        if "PARTY_A" not in parties or "PARTY_B" not in parties:
            raise ValueError("Contract IR requires resolved PARTY_A and PARTY_B")
        if self.contract_ir.our_party not in {parties["PARTY_A"], parties["PARTY_B"]}:
            raise ValueError("Contract IR our_party must match a resolved party")
        if self.contract_ir.counterparty not in {parties["PARTY_A"], parties["PARTY_B"]}:
            raise ValueError("Contract IR counterparty must match a resolved party")
        if self.contract_ir.our_party == self.contract_ir.counterparty:
            raise ValueError("Contract IR parties cannot map to the same side")
        return self


class ReviewStageResult(StrictModel):
    findings: list[Finding] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)


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


class EvidenceVerificationStageResult(ReviewStageResult):
    result_type: Literal["EVIDENCE_VERIFICATION_STAGE_V1"]
    contract_profile: ContractProfile
    overview: str = Field(min_length=1, max_length=10_000)
    relationships: list[None] = Field(default_factory=list, max_length=0)


class FinalizeReviewStageResult(StrictModel):
    result_type: Literal["FINAL_REVIEW_STAGE_V1"]
    schema_version: Literal["1.0"]
    review_id: str = Field(min_length=1, max_length=160)
    business_task_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    contract_profile: ContractProfile
    summary: ReviewSummary
    findings: list[Finding]
    evidences: list[Evidence]
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
    document_id: str = Field(min_length=1, max_length=160)
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party_name: str | None = Field(default=None, max_length=500)
    contract_type: Literal["AUTO"]
    review_attitude: Literal["NEUTRAL"]


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
