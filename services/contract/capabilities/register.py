"""Register the frozen contract.review.run capability with Aituge Framework."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from task_manager.pipeline.errors import StageExecutionError
from task_manager.pipeline.stage_registry import StageExecutionContext, StageServiceResult
from task_manager.result_sink import ResultSinkDelivery, ResultSinkRejectedError


CAPABILITY_ID = "contract-review"
CAPABILITY_DIR = Path(__file__).resolve().parent
TASK_TYPE = "contract.review.run"
PIPELINE_ID = "contract-review-pipeline-v1"
AGENT_ID = "contract-review-neutral-v1"

STAGE_SEQUENCE = {
    "parse_contract": 10,
    "resolve_parties": 20,
    "extract_contract_ir": 30,
    "rights_obligations_review": 40,
    "commercial_terms_review": 50,
    "liability_termination_review": 60,
    "missing_ambiguous_clauses": 70,
    "relation_extraction": 80,
    "verify_evidence": 90,
    "finalize_review": 100,
}

IR_FRAGMENT_STAGE_IDS = (
    "extract_ir_definitions_basics",
    "extract_ir_rights_duties",
    "extract_ir_commercial_terms",
    "extract_ir_liability_termination",
    "extract_ir_special_terms",
)

IR_FRAGMENT_EXTERNAL_STAGE_ID = "extract_contract_ir"

MODEL_REVIEW_STAGE_IDS = {
    "rights_obligations_review",
    "commercial_terms_review",
    "liability_termination_review",
    "missing_ambiguous_clauses",
    "relation_extraction",
}

FROZEN_ASYNC_ERROR_CODES = {
    "CONTRACT_PARSE_FAILED",
    "PARTY_UNRESOLVED",
    "FRAMEWORK_RUN_FAILED",
    "FRAMEWORK_RUN_ORPHANED",
    "FRAMEWORK_RECOVERY_FAILED",
    "RESULT_INVALID",
    "EVIDENCE_INVALID",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractTaskInput(StrictModel):
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


class PipelineContextInput(StrictModel):
    task_input: ContractTaskInput
    artifacts: dict[str, Any] = Field(default_factory=dict)


class ContractDocumentToolInput(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)


class ContractBlocksToolInput(ContractDocumentToolInput):
    block_ids: list[str] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=200, ge=1, le=2000)


class ContractClauseContextToolInput(ContractDocumentToolInput):
    block_id: str = Field(min_length=1, max_length=160)
    before: int = Field(default=1, ge=0, le=5)
    after: int = Field(default=1, ge=0, le=5)


class ContractIrToolInput(ContractDocumentToolInput):
    pass


class PartyValue(StrictModel):
    name: str = Field(min_length=1, max_length=500)


class SourceAnchor(StrictModel):
    anchor_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "SourceAnchor":
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class IrDocument(StrictModel):
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    file_type: Literal["pdf", "docx"]
    page_count: int | None = Field(default=None, ge=1)
    block_count: int = Field(gt=0)
    parser_version: str = Field(min_length=1, max_length=160)
    warnings: list[str] = Field(default_factory=list)


class IrParty(StrictModel):
    role: Literal["PARTY_A", "PARTY_B", "OTHER"]
    name: str = Field(min_length=1, max_length=500)
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class IrDefinition(StrictModel):
    term: str = Field(min_length=1, max_length=500)
    meaning: str = Field(min_length=1, max_length=5000)
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class IrClause(StrictModel):
    clause_id: str = Field(min_length=1, max_length=160)
    clause_no: str | None = Field(default=None, max_length=200)
    clause_type: str = Field(min_length=1, max_length=160)
    heading_path: list[str] = Field(default_factory=list, max_length=30)
    text: str = Field(min_length=1, max_length=100_000)
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class IrSemanticItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=160)
    subject: str | None = Field(default=None, max_length=500)
    predicate: str = Field(min_length=1, max_length=1000)
    object: str | None = Field(default=None, max_length=5000)
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class ContractIr(StrictModel):
    ir_version: Literal["1.0"] = "1.0"
    document: IrDocument
    parties: list[IrParty] = Field(default_factory=list)
    our_party: str | None = Field(default=None, min_length=1, max_length=500)
    counterparty: str | None = Field(default=None, min_length=1, max_length=500)
    contract_type: str = Field(default="AUTO", pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    definitions: list[IrDefinition] = Field(default_factory=list)
    clauses: list[IrClause] = Field(min_length=1)
    rights: list[IrSemanticItem] = Field(default_factory=list)
    obligations: list[IrSemanticItem] = Field(default_factory=list)
    prohibitions: list[IrSemanticItem] = Field(default_factory=list)
    payment_terms: list[IrSemanticItem] = Field(default_factory=list)
    delivery_terms: list[IrSemanticItem] = Field(default_factory=list)
    acceptance_terms: list[IrSemanticItem] = Field(default_factory=list)
    liabilities: list[IrSemanticItem] = Field(default_factory=list)
    termination_terms: list[IrSemanticItem] = Field(default_factory=list)
    confidentiality_terms: list[IrSemanticItem] = Field(default_factory=list)
    intellectual_property_terms: list[IrSemanticItem] = Field(default_factory=list)
    dispute_resolution: list[IrSemanticItem] = Field(default_factory=list)
    dates: list[IrSemanticItem] = Field(default_factory=list)
    amounts: list[IrSemanticItem] = Field(default_factory=list)
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class ContractProfile(StrictModel):
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    party_a: PartyValue
    party_b: PartyValue
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"

    @model_validator(mode="after")
    def validate_perspective(self) -> "ContractProfile":
        expected_our = self.party_a.name if self.perspective == "PARTY_A" else self.party_b.name
        expected_other = self.party_b.name if self.perspective == "PARTY_A" else self.party_a.name
        if self.our_party != expected_our or self.counterparty != expected_other:
            raise ValueError("our_party and counterparty must match perspective")
        return self


class ReviewSummary(StrictModel):
    overview: str = Field(min_length=1, max_length=10_000)
    high_count: int = Field(ge=0)
    medium_count: int = Field(ge=0)
    low_count: int = Field(ge=0)
    info_count: int = Field(ge=0)


class Finding(StrictModel):
    finding_id: str = Field(min_length=1, max_length=160)
    category: Literal[
        "PARTY_IDENTIFICATION",
        "RIGHTS_OBLIGATIONS_IMBALANCE",
        "PAYMENT",
        "DELIVERY",
        "ACCEPTANCE",
        "BREACH",
        "LIABILITY",
        "TERMINATION",
        "CONFIDENTIALITY",
        "INTELLECTUAL_PROPERTY",
        "DISPUTE_RESOLUTION",
        "MISSING_CLAUSE",
        "AMBIGUITY",
        "INTERNAL_CONFLICT",
        "OTHER",
    ]
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    title: str = Field(min_length=1, max_length=500)
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)
    issue: str = Field(min_length=1, max_length=10_000)
    impact_to_our_party: str = Field(min_length=1, max_length=10_000)
    suggestion: str = Field(min_length=1, max_length=10_000)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_evidence_ids(self) -> "Finding":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique")
        return self


class Evidence(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    finding_id: str = Field(min_length=1, max_length=160)
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]
    block_id: str | None = Field(default=None, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    quoted_text: str | None = None
    quoted_text_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    checked_scope: str | None = Field(default=None, min_length=1, max_length=500)
    verification_note: str | None = Field(default=None, min_length=1, max_length=5000)
    bounding_boxes: list[None] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def validate_shape(self) -> "Evidence":
        text_fields = (
            self.block_id,
            self.char_start,
            self.char_end,
            self.quoted_text,
            self.quoted_text_hash,
        )
        if self.evidence_type in {"TEXT_QUOTE", "CONTEXT"}:
            if any(value is None for value in text_fields):
                raise ValueError("Text evidence requires a block, range, text, and hash")
            assert self.char_start is not None and self.char_end is not None
            assert self.quoted_text is not None and self.quoted_text_hash is not None
            if self.char_end <= self.char_start or self.char_end - self.char_start != len(self.quoted_text):
                raise ValueError("Text evidence range must match quoted_text")
            expected = "sha256:" + hashlib.sha256(self.quoted_text.encode("utf-8")).hexdigest()
            if self.quoted_text_hash != expected:
                raise ValueError("quoted_text_hash does not match quoted_text")
        else:
            if self.checked_scope is None or self.verification_note is None:
                raise ValueError("ABSENCE evidence requires checked_scope and verification_note")
            if any(value is not None for value in text_fields) or self.page_number is not None:
                raise ValueError("ABSENCE evidence cannot contain text positioning fields")
        return self


class ParseContractStageResult(StrictModel):
    result_type: Literal["PARSE_CONTRACT_STAGE_V1"]
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    block_count: int = Field(gt=0)
    ir_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PartyResolutionStageResult(StrictModel):
    result_type: Literal["PARTY_RESOLUTION_STAGE_V1"]
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    party_a: PartyValue
    party_b: PartyValue
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


class ContractIrSemanticDelta(StrictModel):
    definitions: list[IrDefinition] = Field(default_factory=list)
    rights: list[IrSemanticItem] = Field(default_factory=list)
    obligations: list[IrSemanticItem] = Field(default_factory=list)
    prohibitions: list[IrSemanticItem] = Field(default_factory=list)
    payment_terms: list[IrSemanticItem] = Field(default_factory=list)
    delivery_terms: list[IrSemanticItem] = Field(default_factory=list)
    acceptance_terms: list[IrSemanticItem] = Field(default_factory=list)
    liabilities: list[IrSemanticItem] = Field(default_factory=list)
    termination_terms: list[IrSemanticItem] = Field(default_factory=list)
    confidentiality_terms: list[IrSemanticItem] = Field(default_factory=list)
    intellectual_property_terms: list[IrSemanticItem] = Field(default_factory=list)
    dispute_resolution: list[IrSemanticItem] = Field(default_factory=list)
    dates: list[IrSemanticItem] = Field(default_factory=list)
    amounts: list[IrSemanticItem] = Field(default_factory=list)


class IrDefinitionsBasicsFragmentResult(StrictModel):
    result_type: Literal["CONTRACT_IR_DEFINITIONS_BASICS_FRAGMENT_V1"]
    definitions: list[IrDefinition] = Field(default_factory=list)
    dates: list[IrSemanticItem] = Field(default_factory=list)
    amounts: list[IrSemanticItem] = Field(default_factory=list)


class IrRightsDutiesFragmentResult(StrictModel):
    result_type: Literal["CONTRACT_IR_RIGHTS_DUTIES_FRAGMENT_V1"]
    rights: list[IrSemanticItem] = Field(default_factory=list)
    obligations: list[IrSemanticItem] = Field(default_factory=list)
    prohibitions: list[IrSemanticItem] = Field(default_factory=list)


class IrCommercialTermsFragmentResult(StrictModel):
    result_type: Literal["CONTRACT_IR_COMMERCIAL_TERMS_FRAGMENT_V1"]
    payment_terms: list[IrSemanticItem] = Field(default_factory=list)
    delivery_terms: list[IrSemanticItem] = Field(default_factory=list)
    acceptance_terms: list[IrSemanticItem] = Field(default_factory=list)


class IrLiabilityTerminationFragmentResult(StrictModel):
    result_type: Literal["CONTRACT_IR_LIABILITY_TERMINATION_FRAGMENT_V1"]
    liabilities: list[IrSemanticItem] = Field(default_factory=list)
    termination_terms: list[IrSemanticItem] = Field(default_factory=list)


class IrSpecialTermsFragmentResult(StrictModel):
    result_type: Literal["CONTRACT_IR_SPECIAL_TERMS_FRAGMENT_V1"]
    confidentiality_terms: list[IrSemanticItem] = Field(default_factory=list)
    intellectual_property_terms: list[IrSemanticItem] = Field(default_factory=list)
    dispute_resolution: list[IrSemanticItem] = Field(default_factory=list)


class ExtractContractIrStageResult(StrictModel):
    result_type: Literal["CONTRACT_IR_STAGE_V1"]
    semantic_ir: ContractIrSemanticDelta


class EvidenceCandidate(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    finding_id: str = Field(min_length=1, max_length=160)
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]
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
        if self.evidence_type in {"TEXT_QUOTE", "CONTEXT"}:
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


class SinkResponseData(StrictModel):
    accepted: bool
    duplicate: bool
    ignored_reason: str | None = None


class SinkResponse(StrictModel):
    success: Literal[True]
    data: SinkResponseData
    request_id: str = Field(min_length=1, max_length=160)


class InternalContractBlock(StrictModel):
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(gt=0)
    block_type: str = Field(min_length=1, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    paragraph_no: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    text: str = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class InternalContractBlocksData(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    blocks: list[InternalContractBlock] = Field(default_factory=list)


class InternalContractBlocksEnvelope(StrictModel):
    success: Literal[True]
    data: InternalContractBlocksData
    request_id: str = Field(min_length=1, max_length=160)


IR_FRAGMENT_MODELS = {
    "extract_ir_definitions_basics": IrDefinitionsBasicsFragmentResult,
    "extract_ir_rights_duties": IrRightsDutiesFragmentResult,
    "extract_ir_commercial_terms": IrCommercialTermsFragmentResult,
    "extract_ir_liability_termination": IrLiabilityTerminationFragmentResult,
    "extract_ir_special_terms": IrSpecialTermsFragmentResult,
}

IR_FRAGMENT_FIELDS = {
    "extract_ir_definitions_basics": ("definitions", "dates", "amounts"),
    "extract_ir_rights_duties": ("rights", "obligations", "prohibitions"),
    "extract_ir_commercial_terms": (
        "payment_terms",
        "delivery_terms",
        "acceptance_terms",
    ),
    "extract_ir_liability_termination": ("liabilities", "termination_terms"),
    "extract_ir_special_terms": (
        "confidentiality_terms",
        "intellectual_property_terms",
        "dispute_resolution",
    ),
}


def _callback_identity(delivery: ResultSinkDelivery) -> tuple[str, int, str, str]:
    task_input = ContractTaskInput.model_validate(delivery.task.input_payload_json or {})
    run_id = str(delivery.task.current_run_id or "").strip()
    if not run_id:
        raise RuntimeError("Contract task has no active Framework Run")
    return task_input.review_id, task_input.attempt_no, delivery.task.id, run_id


def _external_callback_stage(stage_id: str | None) -> str | None:
    if stage_id in IR_FRAGMENT_STAGE_IDS:
        return IR_FRAGMENT_EXTERNAL_STAGE_ID
    return stage_id


def _fragment_result(stage_id: str, value: Any) -> StrictModel:
    try:
        model = IR_FRAGMENT_MODELS[stage_id]
    except KeyError as exc:
        raise RuntimeError(f"Unknown Contract IR fragment stage '{stage_id}'") from exc
    result = model.model_validate(value)
    seen_item_ids: set[str] = set()
    anchor_positions: dict[str, tuple[str, int | None, int, int]] = {}
    for field in IR_FRAGMENT_FIELDS[stage_id]:
        field_semantics: set[tuple[Any, ...]] = set()
        for item in getattr(result, field):
            if isinstance(item, IrSemanticItem):
                if item.item_id in seen_item_ids:
                    raise ValueError(
                        f"Contract IR fragment contains duplicate item_id '{item.item_id}'"
                    )
                seen_item_ids.add(item.item_id)
                semantic_key = (item.subject, item.predicate, item.object)
            else:
                semantic_key = (item.term, item.meaning)
            anchor_key = tuple(
                (anchor.block_id, anchor.page_number, anchor.char_start, anchor.char_end)
                for anchor in item.source_anchors
            )
            exact_key = (*semantic_key, anchor_key)
            if exact_key in field_semantics:
                raise ValueError(f"Contract IR fragment contains a duplicate '{field}' item")
            field_semantics.add(exact_key)
            for anchor in item.source_anchors:
                position = (
                    anchor.block_id,
                    anchor.page_number,
                    anchor.char_start,
                    anchor.char_end,
                )
                previous = anchor_positions.setdefault(anchor.anchor_id, position)
                if previous != position:
                    raise ValueError(
                        f"Contract IR fragment reuses anchor_id '{anchor.anchor_id}' "
                        "for different source positions"
                    )
    return result


def _fragment_anchors(stage_id: str, value: Any) -> list[SourceAnchor]:
    result = _fragment_result(stage_id, value)
    anchors: list[SourceAnchor] = []
    for field in IR_FRAGMENT_FIELDS[stage_id]:
        for item in getattr(result, field):
            anchors.extend(item.source_anchors)
    return anchors


def _merge_contract_ir_fragments_handler():
    async def merge(context: StageExecutionContext) -> StageServiceResult:
        semantic: dict[str, Any] = {}
        for stage_id in IR_FRAGMENT_STAGE_IDS:
            artifact = context.artifacts.get(stage_id)
            if artifact is None or not isinstance(artifact.content_json, dict):
                raise StageExecutionError(
                    f"Contract IR fragment '{stage_id}' is missing.",
                    code="missing_dependency_artifact",
                    retryable=False,
                )
            fragment = _fragment_result(stage_id, artifact.content_json)
            for field in IR_FRAGMENT_FIELDS[stage_id]:
                semantic[field] = getattr(fragment, field)
        result = ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            semantic_ir=ContractIrSemanticDelta.model_validate(semantic),
        )
        return StageServiceResult(
            output=result.model_dump(mode="json"),
            summary="Merged five validated Contract IR fragments.",
        )

    return merge


def _fragment_anchor_validator(base_url: str, token: str):
    async def validate(delivery: ResultSinkDelivery) -> None:
        stage_id = str(delivery.stage_id or "")
        task_input = ContractTaskInput.model_validate(delivery.task.input_payload_json or {})
        anchors = _fragment_anchors(stage_id, delivery.output)
        block_ids = list(dict.fromkeys(anchor.block_id for anchor in anchors))
        blocks: dict[str, InternalContractBlock] = {}
        for offset in range(0, len(block_ids), 200):
            selected = block_ids[offset : offset + 200]
            async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
                response = await client.post(
                    "/v1/internal/contract-tools/blocks",
                    headers={
                        "X-Internal-Service": "aituge-framework",
                        "X-Internal-Token": token,
                        "X-Request-Id": (
                            f"contract-ir-fragment:{delivery.task.current_run_id}:{stage_id}:{offset}"
                        ),
                    },
                    json={
                        "review_id": task_input.review_id,
                        "document_id": task_input.document_id,
                        "block_ids": selected,
                        "limit": max(1, len(selected)),
                    },
                )
                response.raise_for_status()
                envelope = InternalContractBlocksEnvelope.model_validate(response.json())
            if (
                envelope.data.review_id != task_input.review_id
                or envelope.data.document_id != task_input.document_id
            ):
                raise ValueError("Contract IR fragment block scope does not match the task")
            blocks.update({item.block_id: item for item in envelope.data.blocks})

        if set(blocks) != set(block_ids):
            raise ValueError("Contract IR fragment references an unknown contract block")
        for anchor in anchors:
            block = blocks[anchor.block_id]
            if anchor.char_end > len(block.text):
                raise ValueError("Contract IR fragment anchor exceeds its contract block")
            if anchor.page_number != block.page_number:
                raise ValueError("Contract IR fragment anchor page does not match its contract block")

    return validate


def _callback_envelope(delivery: ResultSinkDelivery) -> tuple[str, dict[str, Any]]:
    review_id, attempt_no, task_id, run_id = _callback_identity(delivery)
    if delivery.status == "failed":
        callback_type = "RUN_FAILED"
        internal_stage_id = delivery.stage_id
        stage_id = _external_callback_stage(internal_stage_id)
        sequence = STAGE_SEQUENCE.get(stage_id or "", 900) + 1
        result = None
        framework_error_code = delivery.error_code or "FRAMEWORK_RUN_FAILED"
        domain_error_code = delivery.domain_error_code
        if domain_error_code in FROZEN_ASYNC_ERROR_CODES:
            error_code = domain_error_code
            retryable = delivery.domain_retryable
            user_action_required = delivery.user_action_required
        elif stage_id == "resolve_parties" and framework_error_code in {
            "required_result_sink_failed",
            "PARTY_UNRESOLVED",
        }:
            error_code = "PARTY_UNRESOLVED"
            retryable = False
            user_action_required = True
        elif stage_id in {*MODEL_REVIEW_STAGE_IDS, "verify_evidence"} and framework_error_code in {
            "required_result_sink_failed",
            "EVIDENCE_INVALID",
        }:
            error_code = "EVIDENCE_INVALID"
            retryable = False
            user_action_required = False
        elif framework_error_code in FROZEN_ASYNC_ERROR_CODES:
            error_code = framework_error_code
            retryable = delivery.retryable
            user_action_required = error_code == "PARTY_UNRESOLVED"
        else:
            error_code = "FRAMEWORK_RUN_FAILED"
            retryable = delivery.retryable
            user_action_required = False
        details = dict(delivery.error_details or {})
        if stage_id:
            details["stage_id"] = stage_id
            details["framework_error_code"] = framework_error_code
        if internal_stage_id in IR_FRAGMENT_STAGE_IDS:
            details["internal_stage_id"] = internal_stage_id
        error = {
            "code": error_code,
            "message": (delivery.error_message or "Framework contract stage failed")[:2000],
            "retryable": retryable,
            "user_action_required": user_action_required,
            "details": details or None,
        }
    elif delivery.stage_id is not None:
        if delivery.stage_id not in STAGE_SEQUENCE:
            raise RuntimeError(f"Unknown contract stage '{delivery.stage_id}'")
        callback_type = "STAGE_RESULT"
        stage_id = delivery.stage_id
        sequence = STAGE_SEQUENCE[stage_id]
        result = delivery.output
        error = None
    else:
        callback_type = "RUN_SUCCEEDED"
        stage_id = None
        sequence = 1000
        result = None
        error = None
    callback_id = f"contract:{run_id}:{callback_type}:{stage_id or 'run'}:{sequence}"
    return review_id, {
        "schema_version": "1.0",
        "review_id": review_id,
        "attempt_no": attempt_no,
        "framework_task_id": task_id,
        "framework_run_id": run_id,
        "stage_id": stage_id,
        "event_sequence": sequence,
        "callback_id": callback_id,
        "callback_type": callback_type,
        "result": result,
        "error": error,
    }


def _result_sink_handler(base_url: str, token: str):
    validate_fragment = _fragment_anchor_validator(base_url, token)

    async def deliver(delivery: ResultSinkDelivery) -> None:
        if delivery.stage_id in IR_FRAGMENT_STAGE_IDS and delivery.status != "failed":
            try:
                await validate_fragment(delivery)
            except (httpx.HTTPError, ValueError) as exc:
                raise ResultSinkRejectedError(
                    f"Contract IR fragment validation failed: {exc}",
                    code="RESULT_INVALID",
                    retryable=True,
                    details={"internal_stage_id": delivery.stage_id},
                ) from exc
            return
        review_id, envelope = _callback_envelope(delivery)
        request_id = envelope["callback_id"]
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
                    response = await client.post(
                        f"/v1/internal/contract-reviews/{review_id}/framework-result",
                        headers={
                            "X-Internal-Service": "aituge-framework",
                            "X-Internal-Token": token,
                            "X-Request-Id": request_id,
                        },
                        json=envelope,
                    )
                    response.raise_for_status()
                    SinkResponse.model_validate(response.json())
                return
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:1000].strip()
                suffix = f": {detail}" if detail else ""
                error = RuntimeError(
                    f"Contract Result Sink returned HTTP {exc.response.status_code}{suffix}"
                )
                if exc.response.status_code == 422:
                    try:
                        response_error = exc.response.json().get("error", {})
                    except (AttributeError, ValueError):
                        try:
                            response_error = json.loads(exc.response.text).get("error", {})
                        except (AttributeError, TypeError, ValueError):
                            response_error = {}
                    raise ResultSinkRejectedError(
                        str(error),
                        code=response_error.get("code"),
                        retryable=bool(response_error.get("retryable", False)),
                        user_action_required=bool(
                            response_error.get("user_action_required", False)
                        ),
                        details=(
                            response_error.get("details")
                            if isinstance(response_error.get("details"), dict)
                            else None
                        ),
                    ) from exc
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(0.1)
            except (httpx.RequestError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.1)
        raise RuntimeError(f"Contract Result Sink failed: {last_error}") from last_error

    return deliver


def _stage_gateway_handler(base_url: str, token: str):
    async def execute(context: StageExecutionContext) -> StageServiceResult:
        task_input = ContractTaskInput.model_validate(context.task.input_payload_json or {})
        payload = {
            "schema_version": "1.0",
            "review_id": task_input.review_id,
            "attempt_no": task_input.attempt_no,
            "framework_task_id": context.task.id,
            "framework_run_id": context.run.id,
            "stage_id": context.stage.stage_id,
            "task_input": task_input.model_dump(mode="json"),
            "artifacts": context.stage_input.get("artifacts", {}),
        }
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
                response = await client.post(
                    f"/v1/internal/contract-reviews/{task_input.review_id}/stages/execute",
                    headers={
                        "X-Internal-Service": "aituge-framework",
                        "X-Internal-Token": token,
                        "X-Request-Id": f"stage:{context.run.id}:{context.stage.stage_id}",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            try:
                error = exc.response.json().get("error", {})
            except (AttributeError, ValueError):
                try:
                    error = json.loads(exc.response.text).get("error", {})
                except (AttributeError, TypeError, ValueError):
                    error = {}
            code = str(error.get("code") or "FRAMEWORK_RUN_FAILED")
            if code not in FROZEN_ASYNC_ERROR_CODES:
                code = "FRAMEWORK_RUN_FAILED"
            message = str(
                error.get("message")
                or f"Contract stage gateway returned HTTP {exc.response.status_code}"
            )[:2000]
            raise StageExecutionError(
                message,
                code=code,
                retryable=bool(error.get("retryable", exc.response.status_code >= 500)),
            ) from exc
        except httpx.RequestError as exc:
            raise StageExecutionError(
                f"Contract stage gateway request failed: {exc}",
                code="FRAMEWORK_RUN_FAILED",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise StageExecutionError(
                "Contract stage gateway returned invalid JSON",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            ) from exc
        if body.get("success") is not True or not isinstance(body.get("data"), dict):
            raise StageExecutionError(
                "Contract stage gateway returned an invalid response",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            )
        return StageServiceResult(output=body["data"])

    return execute


async def register(registry, settings) -> None:
    base_url = settings.require("CONTRACT_SERVICE_BASE_URL").rstrip("/")
    callback_token = settings.require("CONTRACT_RESULT_SINK_INTERNAL_TOKEN")
    model_id = settings.get("CONTRACT_MODEL_ID", "deepseek-v4-pro").strip()
    internal_headers = {
        "X-Internal-Service": "aituge-framework",
        "X-Internal-Token": callback_token,
    }

    registry.register_skill_root(CAPABILITY_DIR / "skills")
    for tool_name, path, model, description in (
        ("contract_get_document", "/v1/internal/contract-tools/document", ContractDocumentToolInput,
         "Read technical metadata for the current contract document."),
        ("contract_get_blocks", "/v1/internal/contract-tools/blocks", ContractBlocksToolInput,
         "Read stable normalized contract blocks and source positions."),
        ("contract_get_clause_context", "/v1/internal/contract-tools/clause-context",
         ContractClauseContextToolInput, "Read one contract block with adjacent clause context."),
        ("contract_get_ir", "/v1/internal/contract-tools/ir", ContractIrToolInput,
         "Read the current typed Contract IR for review."),
    ):
        registry.register_http_tool(
            tool_name=tool_name,
            provider="contract_http",
            display_name=tool_name.replace("_", " ").title(),
            description=description,
            base_url=base_url,
            path=path,
            input_model=model,
            headers=internal_headers,
            request_id_header="X-Request-Id",
            timeout_seconds=30,
            max_response_chars=500_000,
        )

    skill_names = [
        "contract-party-resolution",
        "contract-ir-extraction",
        "contract-ir-definitions-basics",
        "contract-ir-rights-duties",
        "contract-ir-commercial-terms",
        "contract-ir-liability-termination",
        "contract-ir-special-terms",
        "contract-rights-obligations",
        "contract-commercial-terms",
        "contract-liability-termination",
        "contract-missing-ambiguity",
        "contract-relation-extraction",
        "contract-neutral-risk-review",
    ]
    for skill_name in skill_names:
        registry.register_skill_package(
            package_name=f"{skill_name}-package",
            display_name=skill_name.replace("-", " ").title(),
            description="Frozen neutral contract-review skill package.",
            tags=["contract", "review", "neutral", "v1"],
            primary_skill=skill_name,
        )

    registry.register_agent(
        agent_id=AGENT_ID,
        name="Contract Review Neutral Agent V1",
        description="Reviews one contract from the selected party perspective with source evidence.",
        model_id=model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the neutral contract-review agent. Stay on the selected PARTY_A or PARTY_B "
            "perspective, use only current-contract tools and artifacts, and return exactly the "
            "registered JSON shape. Never invent source text, block IDs, evidence offsets, or "
            "cryptographic hashes. For review stages, return source coordinates as Evidence "
            "Candidates and let Contract Python materialize the exact quote and SHA-256. "
            "For ABSENCE evidence, every positioning and quoted-text field must be null; only "
            "checked_scope and verification_note describe the verified absence. In each review "
            "stage return at most four highest-materiality findings, keep free-text fields concise, "
            "and merge findings that have the same cause. Do not narrate analysis in the final "
            "answer: its first character must be '{' and its last character must be '}'."
        ),
        default_tools=[
            "contract_get_document",
            "contract_get_blocks",
            "contract_get_clause_context",
            "contract_get_ir",
        ],
    )

    gateway_handler = _stage_gateway_handler(base_url, callback_token)
    registry.register_stage_handler(name="contract_stage_gateway_v1", handler=gateway_handler)
    registry.register_stage_handler(
        name="contract_ir_fragment_merge_v1",
        handler=_merge_contract_ir_fragments_handler(),
    )
    registry.register_result_sink(
        task_type=TASK_TYPE,
        handler=_result_sink_handler(base_url, callback_token),
        required=True,
    )
    registry.register_task(
        task_type=TASK_TYPE,
        name="Contract Review",
        description="Run the frozen neutral contract-review pipeline.",
        handler="pipeline",
        pipeline_id=PIPELINE_ID,
        default_agent_id=AGENT_ID,
        default_skill_package="contract-neutral-risk-review-package",
        default_primary_skill="contract-neutral-risk-review",
        default_tools=[
            "contract_get_document",
            "contract_get_blocks",
            "contract_get_clause_context",
            "contract_get_ir",
        ],
        input_model=ContractTaskInput,
        output_model=FinalizeReviewStageResult,
    )

    review_tools = [
        "contract_get_document",
        "contract_get_blocks",
        "contract_get_clause_context",
        "contract_get_ir",
    ]
    parallel_stages = [
        ("rights_obligations_review", "Review rights and obligations", RightsObligationsStageResult,
         "contract-rights-obligations-package", "contract-rights-obligations"),
        ("commercial_terms_review", "Review payment, delivery and acceptance", CommercialTermsStageResult,
         "contract-commercial-terms-package", "contract-commercial-terms"),
        ("liability_termination_review", "Review liability and termination", LiabilityTerminationStageResult,
         "contract-liability-termination-package", "contract-liability-termination"),
        ("missing_ambiguous_clauses", "Review missing, ambiguous and conflicting clauses",
         MissingAmbiguityStageResult, "contract-missing-ambiguity-package", "contract-missing-ambiguity"),
        ("relation_extraction", "Extract internal clause relationships", RelationExtractionStageResult,
         "contract-relation-extraction-package", "contract-relation-extraction"),
    ]
    ir_fragment_stages = [
        (
            "extract_ir_definitions_basics",
            "Extract definitions, dates and amounts",
            IrDefinitionsBasicsFragmentResult,
            "contract-ir-definitions-basics-package",
            "contract-ir-definitions-basics",
        ),
        (
            "extract_ir_rights_duties",
            "Extract rights, obligations and prohibitions",
            IrRightsDutiesFragmentResult,
            "contract-ir-rights-duties-package",
            "contract-ir-rights-duties",
        ),
        (
            "extract_ir_commercial_terms",
            "Extract payment, delivery and acceptance terms",
            IrCommercialTermsFragmentResult,
            "contract-ir-commercial-terms-package",
            "contract-ir-commercial-terms",
        ),
        (
            "extract_ir_liability_termination",
            "Extract liability and termination terms",
            IrLiabilityTerminationFragmentResult,
            "contract-ir-liability-termination-package",
            "contract-ir-liability-termination",
        ),
        (
            "extract_ir_special_terms",
            "Extract confidentiality, intellectual property and dispute terms",
            IrSpecialTermsFragmentResult,
            "contract-ir-special-terms-package",
            "contract-ir-special-terms",
        ),
    ]
    stages: list[dict[str, Any]] = [
        {
            "stage_id": "parse_contract",
            "name": "Validate persisted contract parse",
            "stage_type": "gateway",
            "input_model": ContractTaskInput,
            "output_model": ParseContractStageResult,
            "input_adapter": "task_input",
            "artifact_type": "contract_parse_result",
            "service_handler": "contract_stage_gateway_v1",
            "timeout_seconds": 60,
            "retry_policy": {"max_attempts": 2, "retry_on": ["timeout"]},
        },
        {
            "stage_id": "resolve_parties",
            "name": "Resolve contract parties and selected perspective",
            "stage_type": "agent",
            "depends_on": ["parse_contract"],
            "input_model": PipelineContextInput,
            "output_model": PartyResolutionStageResult,
            "artifact_type": "contract_party_resolution",
            "agent_id": AGENT_ID,
            "skill_package": "contract-party-resolution-package",
            "primary_skill": "contract-party-resolution",
            "tools": review_tools,
            "output_policy": "repair_once",
            "timeout_seconds": 180,
            "retry_policy": {
                "max_attempts": 2,
                "retry_on": ["invalid_output", "timeout", "required_result_sink_failed"],
            },
        },
    ]
    for stage_id, name, output_model, package, skill in ir_fragment_stages:
        stages.append(
            {
                "stage_id": stage_id,
                "name": name,
                "stage_type": "agent",
                "depends_on": ["parse_contract", "resolve_parties"],
                "input_model": PipelineContextInput,
                "output_model": output_model,
                "artifact_type": f"{stage_id}_result",
                "agent_id": AGENT_ID,
                "skill_package": package,
                "primary_skill": skill,
                "tools": review_tools,
                "output_policy": "repair_once",
                "timeout_seconds": 300,
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": [
                        "invalid_output",
                        "timeout",
                        "required_result_sink_failed",
                    ],
                },
            }
        )
    stages.append(
        {
            "stage_id": "extract_contract_ir",
            "name": "Merge semantic Contract IR fragments",
            "stage_type": "finalizer",
            "depends_on": list(IR_FRAGMENT_STAGE_IDS),
            "input_model": PipelineContextInput,
            "output_model": ExtractContractIrStageResult,
            "artifact_type": "contract_ir",
            "service_handler": "contract_ir_fragment_merge_v1",
            "timeout_seconds": 30,
        }
    )
    for stage_id, name, output_model, package, skill in parallel_stages:
        stages.append(
            {
                "stage_id": stage_id,
                "name": name,
                "stage_type": "agent",
                "depends_on": ["extract_contract_ir"],
                # The complete semantic IR can be very large for 30-100 page contracts.
                # It is already persisted by Contract Python when extract_contract_ir is
                # accepted, so review agents receive only stable task identity here and
                # fetch the IR or selected blocks through the frozen Contract tools.
                "input_model": ContractTaskInput,
                "input_adapter": "task_input",
                "output_model": output_model,
                "artifact_type": f"{stage_id}_result",
                "agent_id": AGENT_ID,
                "skill_package": package,
                "primary_skill": skill,
                "tools": review_tools,
                "output_policy": "repair_once",
                "timeout_seconds": 300,
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["invalid_output", "timeout", "required_result_sink_failed"],
                },
            }
        )
    stages.extend(
        [
            {
                "stage_id": "verify_evidence",
                "name": "Verify evidence and merge findings",
                "stage_type": "gateway",
                "depends_on": [
                    "resolve_parties",
                    "extract_contract_ir",
                    *[item[0] for item in parallel_stages],
                ],
                "input_model": PipelineContextInput,
                "output_model": EvidenceVerificationStageResult,
                "artifact_type": "contract_verified_findings",
                "service_handler": "contract_stage_gateway_v1",
                "timeout_seconds": 60,
            },
            {
                "stage_id": "finalize_review",
                "name": "Finalize stable contract review",
                "stage_type": "finalizer",
                "depends_on": ["verify_evidence"],
                "input_model": PipelineContextInput,
                "output_model": FinalizeReviewStageResult,
                "artifact_type": "contract_review_result",
                "service_handler": "contract_stage_gateway_v1",
                "timeout_seconds": 60,
            },
        ]
    )
    registry.register_pipeline(
        pipeline_id=PIPELINE_ID,
        version="1.0",
        task_type=TASK_TYPE,
        description="Parse, resolve parties, review in parallel, verify evidence, and finalize.",
        final_artifact_type="contract_review_result",
        timeout_seconds=1800,
        resumable=False,
        max_parallelism=3,
        stages=stages,
    )
