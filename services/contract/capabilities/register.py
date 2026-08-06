"""Register the frozen contract.review.run capability with Aituge Framework."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from aituge_model.config import ModelRuntimeProvider

from task_manager.pipeline.errors import StageExecutionError
from task_manager.pipeline.stage_registry import StageExecutionContext, StageServiceResult
from task_manager.result_sink import ResultSinkDelivery, ResultSinkRejectedError
from task_manager.runtime.fencing import verify_current_execution_lease
try:
    from services.contract.capabilities.finding_consolidation import FindingConsolidationEngine
    from services.contract.capabilities.grounded_answer import (
        GroundedAnswerDraft,
        GroundedAnswerPipelineContext,
        GroundedAnswerResult,
        GroundedAnswerTaskInput,
        materialize_grounded_answer,
    )
    from services.contract.capabilities.window_extraction import WindowExtractionEngine
    from services.contract.capabilities.window_pipeline import (
        ContractIrWindowPipeline,
        WindowPipelineError,
        WindowPipelineRequest,
    )
except ModuleNotFoundError as exc:  # standalone capability mount in the runtime image
    if exc.name != "services":
        raise
    from finding_consolidation import FindingConsolidationEngine
    from grounded_answer import (
        GroundedAnswerDraft,
        GroundedAnswerPipelineContext,
        GroundedAnswerResult,
        GroundedAnswerTaskInput,
        materialize_grounded_answer,
    )
    from window_extraction import WindowExtractionEngine
    from window_pipeline import ContractIrWindowPipeline, WindowPipelineError, WindowPipelineRequest


CAPABILITY_ID = "contract-review"
CAPABILITY_DIR = Path(__file__).resolve().parent
TASK_TYPE = "contract.review.run"
PIPELINE_ID = "contract-review-pipeline-v1"
PARTY_RESOLUTION_TASK_TYPE = "contract.party-resolution.run"
PARTY_RESOLUTION_PIPELINE_ID = "contract-party-resolution-pipeline-v1"
AGENT_ID = "contract-review-neutral-v1"
GROUNDED_ANSWER_TASK_TYPE = "contract.grounded.answer"
GROUNDED_ANSWER_PIPELINE_ID = "contract-grounded-answer-pipeline-v1"
GROUNDED_ANSWER_AGENT_ID = "contract-grounded-answer-v1"

STAGE_SEQUENCE = {
    "parse_contract": 10,
    "resolve_parties": 20,
    "extract_contract_ir": 30,
    "finalize_review": 100,
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
    execution_mode: Literal["FULL_REVIEW", "PARTY_RESOLUTION"] = "FULL_REVIEW"
    confirmed_party_a_name: str | None = Field(default=None, min_length=1, max_length=500)
    confirmed_party_b_name: str | None = Field(default=None, min_length=1, max_length=500)
    contract_type: Literal["AUTO"]
    review_attitude: Literal["NEUTRAL"]

    @model_validator(mode="after")
    def validate_confirmed_parties(self) -> "ContractTaskInput":
        if (self.confirmed_party_a_name is None) != (self.confirmed_party_b_name is None):
            raise ValueError("confirmed contract parties must be supplied together")
        if (
            self.confirmed_party_a_name is not None
            and self.confirmed_party_a_name == self.confirmed_party_b_name
        ):
            raise ValueError("confirmed contract parties must be distinct")
        return self


class PipelineContextInput(StrictModel):
    task_input: ContractTaskInput
    artifacts: dict[str, Any] = Field(default_factory=dict)


class ContractDocumentToolInput(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)


class ContractReviewResultToolInput(StrictModel):
    """Compatibility-only model arguments for a task-bound review lookup.

    The grounded-answer runtime deliberately ignores these values and resolves
    both identifiers from the current TaskManager task.  Keeping the optional
    fields avoids rejecting older model calls while preventing an opaque ID
    copied incorrectly by the model from escaping into the contract service.
    """

    review_id: str | None = Field(default=None, min_length=1, max_length=160)
    document_id: str | None = Field(default=None, min_length=1, max_length=160)


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


class InternalContractWindowPlanData(WindowPipelineRequest):
    review_id: str = Field(min_length=1, max_length=160)


class InternalContractWindowPlanEnvelope(StrictModel):
    success: Literal[True]
    data: InternalContractWindowPlanData
    request_id: str = Field(min_length=1, max_length=160)


def _callback_identity(delivery: ResultSinkDelivery) -> tuple[str, int, str, str]:
    task_input = ContractTaskInput.model_validate(delivery.task.input_payload_json or {})
    run_id = str(delivery.task.current_run_id or "").strip()
    if not run_id:
        raise RuntimeError("Contract task has no active Framework Run")
    return task_input.review_id, task_input.attempt_no, delivery.task.id, run_id


def _party_window_context(party: PartyResolutionStageResult) -> str:
    return "\n".join(
        (
            "Validated party context only; never use these values as extraction_text or evidence:",
            f"PARTY_A_NAME={party.party_a.name}",
            f"PARTY_B_NAME={party.party_b.name}",
            f"PERSPECTIVE={party.perspective}",
            f"OUR_PARTY={party.our_party}",
            f"COUNTERPARTY={party.counterparty}",
            f"CONTRACT_TYPE={party.contract_type}",
            "REVIEW_ATTITUDE=NEUTRAL",
        )
    )


_TRAILING_PARENTHETICAL_SUFFIX = re.compile(
    r"^(?P<base>.*\S)\s*[（(][^（）()]+[）)]$"
)


def _normalized_party_name(value: str) -> str:
    """Normalize parser formatting without altering legal-entity punctuation."""
    return " ".join(value.split()).strip().rstrip("|｜").rstrip()


def _unique_party_name(candidates: list[Any], role: str) -> str:
    names: dict[str, str] = {}
    for candidate in candidates:
        if candidate.role != role:
            continue
        # Parser table cells may retain a trailing column separator. It is
        # structural markup, not part of the legal entity name.
        name = _normalized_party_name(candidate.name)
        if name:
            names.setdefault(name.casefold(), name)
    canonical_names: dict[str, str] = {}
    for key, name in names.items():
        suffix = _TRAILING_PARENTHETICAL_SUFFIX.match(name)
        base = _normalized_party_name(suffix.group("base")) if suffix else ""
        canonical_key = base.casefold() if base and base.casefold() in names else key
        canonical_names.setdefault(canonical_key, names.get(canonical_key, name))
    if len(canonical_names) != 1:
        raise StageExecutionError(
            f"Contract has {len(canonical_names)} unambiguous {role} candidates; manual input is required.",
            code="PARTY_UNRESOLVED",
            retryable=False,
        )
    return next(iter(canonical_names.values()))


def _direct_party_resolution_handler(base_url: str, token: str):
    """Resolve explicitly labelled contract parties without an LLM call."""

    async def execute(context: StageExecutionContext) -> StageServiceResult:
        from contract.party import extract_party_candidates

        started_at = time.perf_counter()
        task_input = ContractTaskInput.model_validate(context.task.input_payload_json or {})
        parse_artifact = context.artifacts.get("parse_contract")
        if parse_artifact is None or not isinstance(parse_artifact.content_json, dict):
            raise StageExecutionError(
                "Validated contract parse artifact is missing.",
                code="PARTY_UNRESOLVED",
                retryable=False,
            )
        parsed = ParseContractStageResult.model_validate(parse_artifact.content_json)
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=2.5) as client:
                response = await client.post(
                    "/v1/internal/contract-tools/blocks",
                    headers={
                        "X-Internal-Service": "aituge-framework",
                        "X-Internal-Token": token,
                        "X-Request-Id": (
                            f"contract-party-direct:{context.run.id}:{context.stage.stage_id}"
                        ),
                    },
                    json={
                        "review_id": task_input.review_id,
                        "document_id": task_input.document_id,
                        "block_ids": [],
                        "limit": 2000,
                    },
                )
            response.raise_for_status()
            envelope = InternalContractBlocksEnvelope.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            raise StageExecutionError(
                f"Contract blocks returned HTTP {exc.response.status_code}; manual input is required.",
                code="PARTY_UNRESOLVED",
                retryable=False,
            ) from exc
        except httpx.RequestError as exc:
            raise StageExecutionError(
                "Contract blocks were not available within the party-resolution deadline; "
                "manual input is required.",
                code="PARTY_UNRESOLVED",
                retryable=False,
            ) from exc
        except ValueError as exc:
            raise StageExecutionError(
                "Contract blocks response is invalid; manual input is required.",
                code="PARTY_UNRESOLVED",
                retryable=False,
            ) from exc

        blocks = envelope.data
        if (
            blocks.review_id != task_input.review_id
            or blocks.document_id != task_input.document_id
            or blocks.generation_id != parsed.generation_id
        ):
            raise StageExecutionError(
                "Contract block identity does not match the validated parse; manual input is required.",
                code="PARTY_UNRESOLVED",
                retryable=False,
            )
        candidates = extract_party_candidates(blocks.blocks)
        party_a_name = _unique_party_name(candidates, "PARTY_A")
        party_b_name = _unique_party_name(candidates, "PARTY_B")
        if party_a_name.casefold() == party_b_name.casefold():
            raise StageExecutionError(
                "Resolved contract parties are identical; manual input is required.",
                code="PARTY_UNRESOLVED",
                retryable=False,
            )
        our_party = party_a_name if task_input.perspective == "PARTY_A" else party_b_name
        counterparty = party_b_name if task_input.perspective == "PARTY_A" else party_a_name
        result = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1",
            contract_type="AUTO",
            party_a={"name": party_a_name},
            party_b={"name": party_b_name},
            perspective=task_input.perspective,
            our_party=our_party,
            counterparty=counterparty,
        )
        duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        return StageServiceResult(
            output=result.model_dump(mode="json"),
            summary=f"Resolved explicit contract parties deterministically in {duration_ms} ms.",
            metadata={
                "party_resolution_engine": "deterministic-explicit-labels-v1",
                "duration_ms": duration_ms,
                "candidate_count": len(candidates),
                "model_call_count": 0,
            },
        )

    return execute


def _window_contract_ir_handler(base_url: str, token: str, model_id: str):
    async def execute(context: StageExecutionContext) -> StageServiceResult:
        task_input = ContractTaskInput.model_validate(context.task.input_payload_json or {})
        party_artifact = context.artifacts.get("resolve_parties")
        if party_artifact is None or not isinstance(party_artifact.content_json, dict):
            raise StageExecutionError(
                "Validated party resolution artifact is missing.",
                code="missing_dependency_artifact",
                retryable=False,
            )
        party = PartyResolutionStageResult.model_validate(party_artifact.content_json)
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
                response = await client.post(
                    "/v1/internal/contract-tools/windows",
                    headers={
                        "X-Internal-Service": "aituge-framework",
                        "X-Internal-Token": token,
                        "X-Request-Id": f"contract-window:{context.run.id}:{context.stage.stage_id}",
                    },
                    json={
                        "review_id": task_input.review_id,
                        "document_id": task_input.document_id,
                    },
                )
            response.raise_for_status()
            envelope = InternalContractWindowPlanEnvelope.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            raise StageExecutionError(
                f"Contract Window plan returned HTTP {exc.response.status_code}",
                code="FRAMEWORK_RUN_FAILED",
                retryable=exc.response.status_code >= 500,
            ) from exc
        except httpx.RequestError as exc:
            raise StageExecutionError(
                f"Contract Window plan request failed: {exc}",
                code="FRAMEWORK_RUN_FAILED",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise StageExecutionError(
                "Contract Window plan response is invalid.",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            ) from exc

        plan = envelope.data
        if plan.review_id != task_input.review_id or plan.document_id != task_input.document_id:
            raise StageExecutionError(
                "Contract Window plan identity does not match the task.",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            )
        party_context = _party_window_context(party)
        request = WindowPipelineRequest.model_validate(
            plan.model_dump(
                mode="json",
                exclude={"review_id"},
            )
        )
        request = request.model_copy(
            update={
                "windows": [
                    window.model_copy(
                        update={
                            "context_text": "\n".join(
                                item
                                for item in (party_context, window.context_text)
                                if item
                            )
                        }
                    )
                    for window in request.windows
                ]
            }
        )
        try:
            pipeline_result = await ContractIrWindowPipeline(
                extractor=WindowExtractionEngine(),
            ).run(
                request,
                tenant_id=context.task.tenant_id,
                model_id=model_id,
            )
        except WindowPipelineError as exc:
            raise StageExecutionError(
                str(exc),
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
                domain_error_code=exc.code,
                details=exc.details,
            ) from exc
        result = ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            semantic_ir=ContractIrSemanticDelta.model_validate(
                pipeline_result.semantic_ir.model_dump(mode="json")
            ),
        )
        return StageServiceResult(
            output=result.model_dump(mode="json"),
            summary=(
                f"Extracted Contract IR from {len(request.windows)} windows in "
                f"{pipeline_result.duration_ms} ms; model calls={pipeline_result.model_call_count}, "
                f"retries={pipeline_result.retry_count}."
            ),
            metadata={
                "ir_engine": "window",
                "window_count": len(request.windows),
                "duration_ms": pipeline_result.duration_ms,
                "model_call_count": pipeline_result.model_call_count,
                "retry_count": pipeline_result.retry_count,
                "semantic_ir_hash": pipeline_result.semantic_ir_hash,
            },
        )

    return execute


def _direct_contract_review_handler(base_url: str, token: str, model_id: str):
    """Run the accepted Direct structured review as the formal final stage.

    The final stage ID and DTO remain frozen so Contract Python, the result
    sink, Java callbacks, and the frontend do not need a compatibility change.
    """

    async def execute(context: StageExecutionContext) -> StageServiceResult:
        try:
            from contract.risk.models import RiskReviewPlanInput, RiskSourceBlock
            from contract.api.models import ContractProfile as DirectContractProfile
            from contract.callback.models import (
                ExtractContractIrStageResult as DirectExtractContractIrStageResult,
            )
            from services.contract.capabilities.direct_e2e import (
                DirectRiskReviewEndToEndRequest,
            )
            from services.contract.capabilities.legacy_compatibility import (
                LegacyCompatibilityContext,
            )
            from services.contract.capabilities.party_roles import contract_party_roles
            from services.contract.scripts.contract_risk_stage66_direct_e2e import (
                _execute_one,
            )
        except ImportError as exc:
            raise StageExecutionError(
                f"Direct risk-review runtime is unavailable: {exc}",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            ) from exc

        task_input = ContractTaskInput.model_validate(context.task.input_payload_json or {})
        parse_artifact = context.artifacts.get("parse_contract")
        party_artifact = context.artifacts.get("resolve_parties")
        ir_artifact = context.artifacts.get("extract_contract_ir")
        if any(
            item is None or not isinstance(item.content_json, dict)
            for item in (parse_artifact, party_artifact, ir_artifact)
        ):
            raise StageExecutionError(
                "Direct review dependencies are incomplete.",
                code="missing_dependency_artifact",
                retryable=False,
            )
        parsed = ParseContractStageResult.model_validate(parse_artifact.content_json)
        party = PartyResolutionStageResult.model_validate(party_artifact.content_json)
        stage_result = ExtractContractIrStageResult.model_validate(ir_artifact.content_json)

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
                response = await client.post(
                    "/v1/internal/contract-tools/blocks",
                    headers={
                        "X-Internal-Service": "aituge-framework",
                        "X-Internal-Token": token,
                        "X-Request-Id": f"contract-direct:{context.run.id}:blocks",
                    },
                    json={
                        "review_id": task_input.review_id,
                        "document_id": task_input.document_id,
                        "block_ids": [],
                        "limit": 2000,
                    },
                )
            response.raise_for_status()
            blocks_envelope = InternalContractBlocksEnvelope.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            raise StageExecutionError(
                f"Contract blocks returned HTTP {exc.response.status_code}",
                code="FRAMEWORK_RUN_FAILED",
                retryable=exc.response.status_code >= 500,
            ) from exc
        except httpx.RequestError as exc:
            raise StageExecutionError(
                f"Contract blocks request failed: {exc}",
                code="FRAMEWORK_RUN_FAILED",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise StageExecutionError(
                "Contract blocks response is invalid.",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            ) from exc

        blocks = blocks_envelope.data
        if (
            blocks.review_id != task_input.review_id
            or blocks.document_id != task_input.document_id
            or blocks.generation_id != parsed.generation_id
        ):
            raise StageExecutionError(
                "Direct review block identity does not match the frozen Contract IR.",
                code="FRAMEWORK_RUN_FAILED",
                retryable=False,
            )
        value = RiskReviewPlanInput(
            review_id=task_input.review_id,
            document_id=task_input.document_id,
            generation_id=parsed.generation_id,
            attempt_no=task_input.attempt_no,
            perspective=party.perspective,
            our_party=party.our_party,
            counterparty=party.counterparty,
            contract_type=party.contract_type,
            review_attitude=task_input.review_attitude,
            stage_result=DirectExtractContractIrStageResult.model_validate(
                stage_result.model_dump(mode="json")
            ),
            source_blocks=[
                RiskSourceBlock(
                    block_id=item.block_id,
                    block_no=item.block_no,
                    text=item.text,
                    page_number=item.page_number,
                    heading_path=list(item.heading_path),
                )
                for item in blocks.blocks
                if item.block_type != "footer" and item.text
            ],
            selected_playbook_ids=["base_neutral"],
        )
        roles = contract_party_roles(
            perspective=value.perspective,
            our_party=value.our_party,
            counterparty=value.counterparty,
        )
        compatibility_context = LegacyCompatibilityContext(
            review_id=value.review_id,
            business_task_id=task_input.business_task_id,
            contract_version_id=task_input.contract_version_id,
            generation_id=value.generation_id,
            contract_hash=parsed.ir_hash,
            contract_profile=DirectContractProfile(
                contract_type=value.contract_type,
                party_a={"name": roles.party_a_name},
                party_b={"name": roles.party_b_name},
                perspective=value.perspective,
                our_party=value.our_party,
                counterparty=value.counterparty,
                review_attitude=value.review_attitude,
            ),
        )
        request = DirectRiskReviewEndToEndRequest(
            review_id=value.review_id,
            generation_id=value.generation_id,
            contract_hash=parsed.ir_hash,
            fixture_id=f"formal:{value.document_id}",
            contract_ir_stage_result=stage_result.model_dump(mode="json"),
            risk_review_context={
                "resolve_parties_artifact": party.model_dump(mode="json"),
                "execution_source": "FORMAL_DIRECT_PIPELINE",
            },
        )
        try:
            summary, _attempt, payload, _compatible, _extended = await _execute_one(
                run_index=task_input.attempt_no,
                value=value,
                request=request,
                context=compatibility_context,
                tenant_id=str(context.task.tenant_id),
                model_id=model_id,
                run_id_prefix=f"formal-direct-{context.run.id}",
                allow_dynamic_base_batch_count=True,
                diagnostic_allow_oracle_drift=True,
            )
        except Exception as exc:
            code = getattr(exc, "code", "FRAMEWORK_RUN_FAILED")
            raise StageExecutionError(
                f"Direct risk review failed: {exc}",
                code=code if code in FROZEN_ASYNC_ERROR_CODES else "FRAMEWORK_RUN_FAILED",
                retryable=False,
            ) from exc

        formal = payload.model_dump(mode="json")
        formal.pop("result_hash", None)
        formal["result_type"] = "FINAL_REVIEW_STAGE_V1"
        validated = FinalizeReviewStageResult.model_validate(formal)
        return StageServiceResult(
            output=validated.model_dump(mode="json"),
            summary=(
                f"Direct structured review completed: "
                f"{len(validated.findings)} findings, "
                f"{summary.get('total_model_calls', 0)} model calls."
            ),
            metadata={
                "risk_review_engine": "direct",
                "review_unit_count": 7,
                "check_count": 45,
                "model_calls": summary.get("total_model_calls", 0),
                "repair_calls": summary.get("total_repairs", 0),
                "tool_calls": summary.get("total_tool_calls", 0),
                "core_result_signature": summary.get("core_result_signature"),
            },
        )

    return execute


def _callback_envelope(delivery: ResultSinkDelivery) -> tuple[str, dict[str, Any]]:
    review_id, attempt_no, task_id, run_id = _callback_identity(delivery)
    if delivery.lease_version is None:
        raise RuntimeError("Contract callback requires an active Framework execution lease")
    lease_version = delivery.lease_version
    if delivery.status == "failed":
        callback_type = "RUN_FAILED"
        internal_stage_id = delivery.stage_id
        stage_id = internal_stage_id
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
        elif stage_id == "finalize_review" and framework_error_code in {
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
    callback_id = (
        f"contract:{run_id}:lease:{lease_version}:{callback_type}:{stage_id or 'run'}:{sequence}"
    )
    return review_id, {
        "schema_version": "1.0",
        "review_id": review_id,
        "attempt_no": attempt_no,
        "framework_task_id": task_id,
        "framework_run_id": run_id,
        "stage_id": stage_id,
        "event_sequence": sequence,
        "lease_version": lease_version,
        "callback_id": callback_id,
        "callback_type": callback_type,
        "result": result,
        "error": error,
    }


def _result_sink_handler(base_url: str, token: str):
    async def deliver(delivery: ResultSinkDelivery) -> None:
        review_id, envelope = _callback_envelope(delivery)
        request_id = envelope["callback_id"]
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await verify_current_execution_lease(delivery.task.current_run_id or "")
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


def _stage_gateway_handler(
    base_url: str,
    token: str,
    model_id: str | None = None,
    consolidation_engine: FindingConsolidationEngine | None = None,
):
    engine = consolidation_engine or FindingConsolidationEngine()
    resolved_model_id = (
        model_id
        or ModelRuntimeProvider.from_environment().active_pack.llm.id
    )

    async def execute(context: StageExecutionContext) -> StageServiceResult:
        task_input = ContractTaskInput.model_validate(context.task.input_payload_json or {})
        artifacts = dict(context.stage_input.get("artifacts", {}))
        consolidation_metadata: dict[str, Any] = {}
        if context.stage.stage_id == "verify_evidence":
            consolidation = await engine.consolidate(
                artifacts,
                tenant_id=str(getattr(context.task, "tenant_id", None) or "0"),
                model_id=resolved_model_id,
            )
            artifacts["contract_finding_consolidation"] = consolidation
            consolidation_metadata = {
                "finding_consolidation_status": consolidation["status"],
                "finding_consolidation_candidates": consolidation["candidate_count"],
                "finding_consolidation_model_calls": consolidation["model_call_count"],
            }
        payload = {
            "schema_version": "1.0",
            "review_id": task_input.review_id,
            "attempt_no": task_input.attempt_no,
            "framework_task_id": context.task.id,
            "framework_run_id": context.run.id,
            "stage_id": context.stage.stage_id,
            "task_input": task_input.model_dump(mode="json"),
            "artifacts": artifacts,
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


def _grounded_answer_finalizer(base_url: str, token: str):
    async def finalize(context: StageExecutionContext) -> StageServiceResult:
        task_input = GroundedAnswerTaskInput.model_validate(
            context.task.input_payload_json or {}
        )
        draft_artifact = context.artifacts.get("generate_grounded_answer")
        if draft_artifact is None or not isinstance(draft_artifact.content_json, dict):
            raise StageExecutionError(
                "Grounded answer draft artifact is missing.",
                code="missing_dependency_artifact",
                retryable=False,
            )
        draft = GroundedAnswerDraft.model_validate(draft_artifact.content_json)
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
                response = await client.post(
                    "/v1/internal/contract-tools/review-result",
                    headers={
                        "X-Internal-Service": "aituge-framework",
                        "X-Internal-Token": token,
                        "X-Request-Id": (
                            f"contract-grounded:{context.run.id}:{context.stage.stage_id}"
                        ),
                    },
                    json={
                        "review_id": task_input.review_id,
                        "document_id": task_input.document_id,
                    },
                )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            review_result = data.get("result") if isinstance(data, dict) else None
            if (
                not isinstance(body, dict)
                or body.get("success") is not True
                or not isinstance(review_result, dict)
            ):
                raise ValueError("invalid review result envelope")
            result = materialize_grounded_answer(
                task_input=task_input,
                draft=draft,
                review_result=review_result,
            )
        except httpx.HTTPStatusError as exc:
            raise StageExecutionError(
                f"Contract review result returned HTTP {exc.response.status_code}",
                code="CONTRACT_CONTEXT_UNAVAILABLE",
                retryable=exc.response.status_code >= 500,
            ) from exc
        except httpx.RequestError as exc:
            raise StageExecutionError(
                f"Contract review result request failed: {exc}",
                code="CONTRACT_CONTEXT_UNAVAILABLE",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise StageExecutionError(
                f"Grounded answer citation validation failed: {exc}",
                code="GROUNDING_INVALID",
                retryable=False,
            ) from exc
        return StageServiceResult(
            output=result.model_dump(mode="json"),
            summary="Validated report citations against authoritative contract evidence.",
        )

    return finalize


async def _load_bound_grounded_answer_input(
    *,
    task_id: str,
    tenant_id: str,
) -> GroundedAnswerTaskInput:
    """Load authoritative identifiers for one grounded-answer tool call."""

    from db.db_context import create_db_session
    from task_manager.models import TaskEntity

    async with create_db_session() as session:
        task = await session.get(TaskEntity, task_id)
    if task is None:
        raise RuntimeError("The current grounded-answer task is unavailable.")
    if task.tenant_id != tenant_id:
        raise RuntimeError("The current grounded-answer task belongs to another tenant.")
    if task.task_type != GROUNDED_ANSWER_TASK_TYPE:
        raise RuntimeError("The current task is not a grounded-answer task.")
    try:
        return GroundedAnswerTaskInput.model_validate(task.input_payload_json or {})
    except ValueError as exc:
        raise RuntimeError("The current grounded-answer task input is invalid.") from exc


def _grounded_review_result_tool_factory(base_url: str, token: str):
    """Create a review-result tool bound to its TaskManager task input."""

    def create_bundle(config: Any):
        from llama_index.core.tools.function_tool import FunctionTool
        from tool.bundle import ToolBundle

        publisher = config.config.get("artifact_publisher")

        async def get_review_result(
            review_id: str | None = None,
            document_id: str | None = None,
        ) -> str:
            # Model-supplied identifiers are intentionally ignored.  Opaque
            # business IDs must always come from the trusted task envelope.
            del review_id, document_id
            task_id = str(getattr(publisher, "task_id", "") or "").strip()
            if not task_id:
                raise RuntimeError("The current grounded-answer task context is unavailable.")
            task_input = await _load_bound_grounded_answer_input(
                task_id=task_id,
                tenant_id=config.tenant_id,
            )
            headers = {
                "X-Internal-Service": "aituge-framework",
                "X-Internal-Token": token,
                "X-Tenant-ID": config.tenant_id,
                "X-Request-Id": (
                    f"contract-grounded-tool:{task_id}:"
                    f"{getattr(publisher, 'stage_run_id', 'unknown')}"
                ),
            }
            try:
                async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
                    response = await client.post(
                        "/v1/internal/contract-tools/review-result",
                        headers=headers,
                        json={
                            "review_id": task_input.review_id,
                            "document_id": task_input.document_id,
                        },
                    )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500].strip()
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(
                    "contract_get_review_result failed with HTTP "
                    f"{exc.response.status_code}{suffix}"
                ) from exc
            except httpx.RequestError as exc:
                raise RuntimeError(
                    "contract_get_review_result service is unavailable "
                    f"({exc.__class__.__name__})."
                ) from exc
            if len(response.text) > 500_000:
                raise RuntimeError(
                    "contract_get_review_result response exceeds the configured size limit."
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "contract_get_review_result returned an invalid JSON response."
                ) from exc
            return json.dumps(result, ensure_ascii=False)

        tool = FunctionTool.from_defaults(
            async_fn=get_review_result,
            name="contract_get_review_result",
            description=(
                "Read the completed, validated review result for the current grounded-answer "
                "task. Runtime binds the authoritative review and document identifiers; any "
                "identifier arguments supplied by the model are ignored."
            ),
            fn_schema=ContractReviewResultToolInput,
            return_direct=False,
        )
        return ToolBundle.from_tools([tool])

    return create_bundle


async def register(registry, settings) -> None:
    base_url = settings.require("CONTRACT_SERVICE_BASE_URL").rstrip("/")
    callback_token = settings.require("CONTRACT_RESULT_SINK_INTERNAL_TOKEN")
    model_runtime = ModelRuntimeProvider.from_environment(
        directory=settings.get("MODEL_CONFIG_DIR"),
        pack_id=settings.get("MODEL_PACK_ID"),
    )
    active_pack = model_runtime.active_pack
    model_id = active_pack.llm.id
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

    registry.register_local_tool(
        tool_name="contract_get_review_result",
        provider="contract_task_bound",
        display_name="Contract Get Review Result",
        description=(
            "Read the completed, validated contract review result and source evidences for the "
            "current grounded-answer task."
        ),
        factory=_grounded_review_result_tool_factory(base_url, callback_token),
        llm_tool_names=["contract_get_review_result"],
    )

    skill_names = [
        "contract-party-resolution",
        "contract-neutral-risk-review",
        "contract-grounded-answer",
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
        model_id=model_id,
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

    registry.register_agent(
        agent_id=GROUNDED_ANSWER_AGENT_ID,
        name="Contract Grounded Answer Agent V1",
        description="Generates contract reports and answers grounded in validated review evidence.",
        model_id=model_id,
        system_prompt=(
            "You generate grounded contract content from the completed review result. "
            "For CHAT greetings, assistant-identity questions, or usage questions that do not "
            "ask for any contract-specific fact, do not call tools; answer briefly and generically "
            "with an empty citations list. "
            "Use only contract tools and the supplied task. Every clickable source reference "
            "must use Markdown form [label](#docref-EVIDENCE_ID), and every such link "
            "must have a matching citations entry. Only TEXT_QUOTE or CONTEXT evidence "
            "may be cited; never create a link for ABSENCE evidence. Do not invent identifiers, "
            "quotes, offsets, hashes, parties, findings, or legal conclusions. Return exactly "
            "the registered JSON shape with no prose outside JSON."
        ),
        default_tools=[
            "contract_get_review_result",
        ],
    )

    gateway_handler = _stage_gateway_handler(base_url, callback_token, model_id)
    registry.register_stage_handler(name="contract_stage_gateway_v1", handler=gateway_handler)
    registry.register_stage_handler(
        name="contract_party_resolution_direct_v1",
        handler=_direct_party_resolution_handler(base_url, callback_token),
    )
    registry.register_stage_handler(
        name="contract_ir_window_v1",
        handler=_window_contract_ir_handler(base_url, callback_token, model_id),
    )
    registry.register_stage_handler(
        name="contract_direct_review_v1",
        handler=_direct_contract_review_handler(base_url, callback_token, model_id),
    )
    registry.register_stage_handler(
        name="contract_grounded_answer_finalize_v1",
        handler=_grounded_answer_finalizer(base_url, callback_token),
    )
    registry.register_result_sink(
        task_type=TASK_TYPE,
        handler=_result_sink_handler(base_url, callback_token),
        required=True,
    )
    registry.register_result_sink(
        task_type=PARTY_RESOLUTION_TASK_TYPE,
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
    registry.register_task(
        task_type=PARTY_RESOLUTION_TASK_TYPE,
        name="Contract Party Resolution",
        description="Parse a contract and resolve its two signing parties before review starts.",
        handler="pipeline",
        pipeline_id=PARTY_RESOLUTION_PIPELINE_ID,
        default_agent_id=AGENT_ID,
        default_skill_package="contract-party-resolution-package",
        default_primary_skill="contract-party-resolution",
        default_tools=[
            "contract_get_document",
            "contract_get_blocks",
            "contract_get_clause_context",
            "contract_get_ir",
        ],
        input_model=ContractTaskInput,
        output_model=PartyResolutionStageResult,
    )
    registry.register_task(
        task_type=GROUNDED_ANSWER_TASK_TYPE,
        name="Contract Grounded Answer",
        description="Generate a source-grounded report or answer from a completed contract review.",
        handler="pipeline",
        pipeline_id=GROUNDED_ANSWER_PIPELINE_ID,
        default_agent_id=GROUNDED_ANSWER_AGENT_ID,
        default_skill_package="contract-grounded-answer-package",
        default_primary_skill="contract-grounded-answer",
        default_tools=[
            "contract_get_review_result",
        ],
        stream_chunk_chars=24,
        input_model=GroundedAnswerTaskInput,
        output_model=GroundedAnswerResult,
    )

    review_tools = [
        "contract_get_document",
        "contract_get_blocks",
        "contract_get_clause_context",
        "contract_get_ir",
    ]
    review_party_resolution_stages: list[dict[str, Any]] = [
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
    party_resolution_stages: list[dict[str, Any]] = [
        {
            "stage_id": "parse_contract",
            "name": "Validate persisted contract parse for fast party resolution",
            "stage_type": "gateway",
            "input_model": ContractTaskInput,
            "output_model": ParseContractStageResult,
            "input_adapter": "task_input",
            "artifact_type": "contract_parse_result",
            "service_handler": "contract_stage_gateway_v1",
            "timeout_seconds": 2,
            "retry_policy": {"max_attempts": 1, "retry_on": []},
        },
        {
            "stage_id": "resolve_parties",
            "name": "Resolve explicitly labelled contract parties without a model",
            "stage_type": "finalizer",
            "depends_on": ["parse_contract"],
            "input_model": PipelineContextInput,
            "output_model": PartyResolutionStageResult,
            "artifact_type": "contract_party_resolution",
            "service_handler": "contract_party_resolution_direct_v1",
            "timeout_seconds": 3,
            "retry_policy": {"max_attempts": 1, "retry_on": []},
        },
    ]
    stages: list[dict[str, Any]] = [*review_party_resolution_stages]
    stages.append(
        {
            "stage_id": "extract_contract_ir",
            "name": "Extract semantic Contract IR by source windows",
            "stage_type": "finalizer",
            "depends_on": ["parse_contract", "resolve_parties"],
            "input_model": PipelineContextInput,
            "output_model": ExtractContractIrStageResult,
            "artifact_type": "contract_ir",
            "service_handler": "contract_ir_window_v1",
            "timeout_seconds": 600,
            "retry_policy": {
                "max_attempts": 1,
                "retry_on": [],
            },
        }
    )
    stages.extend(
        [
            {
                "stage_id": "finalize_review",
                "name": "Run Direct structured review and finalize",
                "stage_type": "finalizer",
                "depends_on": [
                    "parse_contract",
                    "resolve_parties",
                    "extract_contract_ir",
                ],
                "input_model": PipelineContextInput,
                "output_model": FinalizeReviewStageResult,
                "artifact_type": "contract_review_result",
                "service_handler": "contract_direct_review_v1",
                "timeout_seconds": 900,
                "retry_policy": {
                    "max_attempts": 1,
                    "retry_on": [],
                },
            },
        ]
    )
    registry.register_pipeline(
        pipeline_id=PIPELINE_ID,
        version="1.0",
        task_type=TASK_TYPE,
        description="Parse, resolve parties, extract IR, run Direct review, and finalize.",
        final_artifact_type="contract_review_result",
        timeout_seconds=1800,
        resumable=False,
        max_parallelism=7,
        stages=stages,
    )
    registry.register_pipeline(
        pipeline_id=PARTY_RESOLUTION_PIPELINE_ID,
        version="1.0",
        task_type=PARTY_RESOLUTION_TASK_TYPE,
        description="Parse a contract and resolve PARTY_A and PARTY_B without starting risk review.",
        final_artifact_type="contract_party_resolution",
        timeout_seconds=5,
        resumable=False,
        max_parallelism=1,
        stages=party_resolution_stages,
    )
    registry.register_pipeline(
        pipeline_id=GROUNDED_ANSWER_PIPELINE_ID,
        version="1.0",
        task_type=GROUNDED_ANSWER_TASK_TYPE,
        description="Generate one grounded report or answer and materialize authoritative citations.",
        final_artifact_type="contract_grounded_answer",
        timeout_seconds=360,
        resumable=False,
        max_parallelism=1,
        stages=[
            {
                "stage_id": "generate_grounded_answer",
                "name": "Generate grounded contract content",
                "stage_type": "agent",
                "input_model": GroundedAnswerTaskInput,
                "output_model": GroundedAnswerDraft,
                "input_adapter": "task_input",
                "artifact_type": "contract_grounded_answer_draft",
                "agent_id": GROUNDED_ANSWER_AGENT_ID,
                "skill_package": "contract-grounded-answer-package",
                "primary_skill": "contract-grounded-answer",
                "tools": [
                    "contract_get_review_result",
                ],
                "output_policy": "repair_once",
                "timeout_seconds": 300,
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["invalid_output", "timeout"],
                },
            },
            {
                "stage_id": "finalize_grounded_answer",
                "name": "Validate and materialize contract citations",
                "stage_type": "finalizer",
                "depends_on": ["generate_grounded_answer"],
                "input_model": GroundedAnswerPipelineContext,
                "output_model": GroundedAnswerResult,
                "artifact_type": "contract_grounded_answer",
                "service_handler": "contract_grounded_answer_finalize_v1",
                "timeout_seconds": 30,
                "retry_policy": {"max_attempts": 1, "retry_on": []},
            },
        ],
    )
