"""Strict Direct Structured Review for the commercial-financial review unit."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from contract.legal_evidence.models import LegalEvidence
from contract.risk.review_ledger import CheckTaskScope
from contract.legal_evidence.prompting import (
    compact_legal_evidence_catalog,
    legal_evidence_ids_for_check,
    remaining_legal_prompt_budget,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from service.conversation.llm_runner import LlmCompletionResult, LlmRuntime
from task_manager.output_parser import parse_json_output

from services.contract.capabilities.model_observation import (
    deferred_completion_kwargs,
    finalize_completion_success,
    finalize_completion_validation_failed,
)
from services.contract.capabilities.party_roles import (
    contract_party_roles,
    text_names_role,
)
from services.contract.capabilities.prompt_budget import (
    PROVIDER_PROMPT_HARD_LIMIT_TOKENS,
    PromptBudgetResult,
    evaluate_prompt_budget,
)
from services.contract.capabilities.review_output_diagnostics import (
    structural_changes, validation_issues, write_private_diagnostic,
)
from services.contract.capabilities.commercial_output_repair import build_check_repair, merge_check_repair

COMMERCIAL_UNIT_ID = "commercial_financial"
COMMERCIAL_CHECK_CODES = tuple(f"CF-{index:03d}" for index in range(1, 9))
BASE_UNIT_ID_PATTERN = (
    r"^(formation_validity_authority|commercial_financial|performance_obligations|"
    r"ip_confidentiality_data|liability_remedies_exit|cross_clause_consistency|"
    r"missing_ambiguity_completeness)$"
)
BASE_CHECK_CODE_PATTERN = (
    r"^(FVA|CF|PO|ICD|LRE|CCC|MAC)-[0-9]{3}$"
)

_SYSTEM_PROMPT = """你是合同商务财务风险直接审查器。
只审查输入分配的CF检查，禁止工具和其他领域；严格站在our_party立场，以NEUTRAL标准识别有原文依据的实质风险。
Finding只代表对our_party不利的风险；有利、中性或一般说明不得生成Finding。每个Check独立审查，不得因其他Check引用同一条款而跳过。
严格遵守输入的decision_policies和output_contract；不得固定限制Finding数量，不得输出Markdown、分析过程或Python负责的技术字段。
decision_note必须提供简短、非空的判断依据；这是JSON内的必要字段，不是要求输出思维过程。
只输出一个JSON对象，第一字符必须是{，最后字符必须是}。"""


@dataclass(frozen=True, slots=True)
class CommercialDecisionPolicy:
    check_code: str
    review_object: str
    triggers: tuple[str, ...]
    non_risk_examples: tuple[str, ...]
    minimum_evidence: str
    category: str
    risk_level_rule: str
    boundary: str


COMMERCIAL_DECISION_POLICIES = {
    "CF-003": CommercialDecisionPolicy(
        check_code="CF-003",
        review_object="发票类型、含税口径、税负承担、开票时限及其与付款条件的先后关系",
        triggers=(
            "我方付款义务早于对方提供合法有效发票，或付款不以取得约定发票为前提",
            "含税/不含税口径、税负承担或税率变化机制不明，且可能增加我方实际付款",
            "发票要求与付款条件冲突，或对我方设置不可控、无期限的开票前置条件",
        ),
        non_risk_examples=(
            "总价明确为含税价，且对方应在我方付款前提供合法有效的约定发票",
            "仅未写明具体税率，但含税总价、发票类型和付款前开票义务均明确",
            "条款对我方有利或只描述正常开票流程，不产生额外付款或税务暴露",
        ),
        minimum_evidence=(
            "现有条款风险必须引用发票/税费/付款条件原文；纯缺失风险才可使用ABSENCE，"
            "并说明已检查的发票、税费与付款范围"
        ),
        category="PAYMENT",
        risk_level_rule=(
            "可能实质增加价款或使我方先付款后长期无法取得合法发票为HIGH；"
            "含税口径、税负或开票时限存在可执行性不确定为MEDIUM；"
            "仅轻微行政性瑕疵为LOW；有利或中性条款不得输出Finding"
        ),
        boundary=(
            "基础价款/总额归CF-001，付款时点归CF-002，调价抵扣归CF-004，"
            "预付款保障归CF-005；CF-003只处理发票和税费造成的独立风险"
        ),
    ),
    "CF-004": CommercialDecisionPolicy(
        check_code="CF-004",
        review_object="调价、考核扣款、抵销、费用扣减及结算调整的触发条件、公式和法律后果",
        triggers=(
            "对方可单方提高价款、增加费用或改变结算结果，且缺少客观触发条件、公式或上限",
            "考核、扣款或费用调整条款句意不完整，未说明调整方式或法律后果，导致我方权利不可执行",
            "不同调价、扣款或结算条款互相冲突，可能造成我方重复付款或无法结算",
        ),
        non_risk_examples=(
            "明确、可执行的扣款或抵销权归属于我方，且计算依据和程序完整",
            "合同采用固定总价且不存在可增加我方付款的调整机制",
            "仅仅没有约定一般抵销权，不得单独认定为风险",
        ),
        minimum_evidence=(
            "必须引用导致调价、扣款、抵销或结算不确定的具体条款；"
            "不得仅用ABSENCE或仅因合同未写一般抵销权而生成Finding"
        ),
        category="PAYMENT",
        risk_level_rule=(
            "对方可无限或重大增加我方付款为HIGH；调整公式、触发条件或法律后果不完整为MEDIUM；"
            "仅程序或通知瑕疵为LOW；完整且有利于我方的扣款权不得输出Finding"
        ),
        boundary=(
            "基础价款归CF-001，付款期限归CF-002，发票税费归CF-003，"
            "预付款保障归CF-005；CF-004只处理结算后的价格调整、扣减和抵销机制"
        ),
    ),
    "CF-005": CommercialDecisionPolicy(
        check_code="CF-005",
        review_object="我方在履约、交付或验收完成前支付价款的比例及对应履约/返还保障",
        triggers=(
            "第一步确认我方是否在主要履约、交付或验收前支付全部或绝大部分价款",
            "第二步确认是否缺少履约保函、保证金、分期/里程碑付款、验收挂钩、退款返还、托管、担保或等效保障",
            "两步同时成立时必须输出ADVANCE_PAYMENT_SECURITY_RISK，不得因CF-002已报告付款前置而跳过",
        ),
        non_risk_examples=(
            "付款按交付或验收里程碑分期，且大部分价款在履约完成后支付",
            "存在足额、可执行的履约保函、保证金、退款返还、托管、担保或等效保障",
            "如果存在有效保障，返回REVIEWED和空findings，并在decision_note说明保障类型",
        ),
        minimum_evidence=(
            "风险Finding必须同时包含：证明提前支付比例/时点的TEXT_QUOTE或CONTEXT，"
            "以及证明已检查合同但未发现有效保障的ABSENCE"
        ),
        category="PAYMENT",
        risk_level_rule=(
            "全部或至少70%价款在主要履约/交付/验收前支付且无有效保障为HIGH；"
            "30%至不足70%的实质预付款无保障，或保障明显不足/不清为MEDIUM；"
            "保障仅有轻微期限或程序瑕疵为LOW"
        ),
        boundary=(
            "付款日期和期限归CF-002；CF-005必须独立判断提前付款之后能否实际保障履约或返还，"
            "同一付款条款可同时支撑CF-002与CF-005，但风险根因不同"
        ),
    ),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommercialCheckSpec(StrictModel):
    check_code: str = Field(pattern=r"^CF-[0-9]{3}$")
    review_question: str = Field(min_length=1, max_length=2000)
    allowed_categories: list[Literal["PAYMENT", "DELIVERY", "ACCEPTANCE"]] = Field(
        min_length=1,
        max_length=1,
    )
    allowed_risk_types: list[str] = Field(min_length=1, max_length=10)
    criticality: Literal["REQUIRED"]


class CommercialSourceAnchor(StrictModel):
    anchor_id: str = Field(min_length=1, max_length=160)


class CommercialIrItem(StrictModel):
    ir_type: Literal[
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
    item_id: str = Field(min_length=1, max_length=160)
    subject: str | None = None
    predicate: str = Field(min_length=1)
    object: str | None = None
    source_anchors: list[CommercialSourceAnchor] = Field(min_length=1)


class CommercialSourceExcerpt(StrictModel):
    anchor_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    heading_path: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_quote_hash(self) -> "CommercialSourceExcerpt":
        if self.char_end - self.char_start != len(self.quoted_text):
            raise ValueError("Source excerpt range must match quoted_text")
        expected = "sha256:" + hashlib.sha256(self.quoted_text.encode("utf-8")).hexdigest()
        if self.quoted_text_hash != expected:
            raise ValueError("Source excerpt hash must match quoted_text")
        return self


class CommercialReviewRequest(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    plan_id: str = Field(pattern=r"^risk-plan-[0-9a-f]{32}$")
    context_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    unit_id: Literal["commercial_financial"]
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    review_attitude: Literal["NEUTRAL"]
    assigned_check_specs: list[CommercialCheckSpec] = Field(min_length=1)
    check_task_scopes: list[CheckTaskScope] = Field(default_factory=list)
    definitions: list[CommercialIrItem] = Field(default_factory=list)
    projected_ir_items: list[CommercialIrItem] = Field(default_factory=list)
    source_excerpts: list[CommercialSourceExcerpt] = Field(min_length=1)
    estimated_input_tokens: int = Field(ge=1, le=6000)
    # Evidence count follows legal-issue coverage. Prompt text is bounded by
    # deterministic compression, not by silently dropping evidence records.
    legal_evidence: list[LegalEvidence] = Field(default_factory=list)
    legal_evidence_input_tokens: int = Field(default=0, ge=0)
    legal_evidence_prompt_status: Literal[
        "NOT_REQUESTED", "INCLUDED", "OMITTED_TOKEN_BUDGET"
    ] = "NOT_REQUESTED"

    @model_validator(mode="after")
    def validate_identity_and_coverage(self) -> "CommercialReviewRequest":
        check_codes = [item.check_code for item in self.assigned_check_specs]
        if len(check_codes) != len(set(check_codes)):
            raise ValueError("Assigned commercial Check codes must be unique")
        item_ids = [item.item_id for item in [*self.definitions, *self.projected_ir_items]]
        anchor_ids = [item.anchor_id for item in self.source_excerpts]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Commercial IR item IDs must be unique")
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("Commercial source Anchor IDs must be unique")
        known_anchors = set(anchor_ids)
        if any(
            anchor.anchor_id not in known_anchors
            for item in [*self.definitions, *self.projected_ir_items]
            for anchor in item.source_anchors
        ):
            raise ValueError("Commercial IR references an unknown source Anchor")
        return self


class ModelEvidenceDraft(StrictModel):
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]
    ir_ref: str | None = Field(default=None, pattern=r"^I[0-9]{3}$")
    evidence_ref: str | None = Field(default=None, pattern=r"^A[0-9]{3}$")
    checked_scope: str | None = Field(default=None, min_length=1, max_length=500)
    verification_note: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> "ModelEvidenceDraft":
        if self.evidence_type == "ABSENCE":
            if self.ir_ref is not None or self.evidence_ref is not None:
                raise ValueError("ABSENCE cannot reference source text")
            if self.checked_scope is None or self.verification_note is None:
                raise ValueError("ABSENCE requires checked_scope and verification_note")
        elif self.ir_ref is None or self.evidence_ref is None:
            raise ValueError("Text evidence requires ir_ref and evidence_ref")
        return self


class ModelFindingDraft(StrictModel):
    check_code: str = Field(pattern=r"^CF-[0-9]{3}$")
    category: Literal["PAYMENT", "DELIVERY", "ACCEPTANCE"]
    risk_type: str = Field(min_length=1, max_length=160)
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    title: str = Field(min_length=1, max_length=300)
    issue: str = Field(min_length=1, max_length=2000)
    impact_to_our_party: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=2000)
    evidence: list[ModelEvidenceDraft] = Field(min_length=1)


class ModelCheckCoverageResultRaw(StrictModel):
    check_code: str = Field(pattern=r"^CF-[0-9]{3}$")
    status: Literal["REVIEWED", "NOT_APPLICABLE", "FAILED"]
    reason_code: str | None = None
    decision_note: str = Field(min_length=1, max_length=1000)
    findings: list[ModelFindingDraft] = Field(default_factory=list)
    candidate_decision: Literal[
        "RISK_CONFIRMED",
        "RISK_NOT_CONFIRMED",
        "TRIGGER_NOT_MET",
        "INSUFFICIENT_EVIDENCE",
    ] | None = None
    identified_security_mechanisms: list[str] | None = Field(default=None, max_length=20)
    candidate_evidence: list[ModelEvidenceDraft] | None = Field(default=None)

    @model_validator(mode="after")
    def validate_candidate_decision(self) -> "ModelCheckCoverageResultRaw":
        if self.check_code == "CF-005":
            if self.candidate_decision is None:
                raise ValueError("CF-005 requires candidate_decision")
            if (
                self.identified_security_mechanisms is None
                or self.candidate_evidence is None
            ):
                raise ValueError("CF-005 requires both candidate detail lists")
            if self.candidate_decision == "RISK_CONFIRMED" and not self.findings:
                raise ValueError("Confirmed CF-005 risk requires at least one Finding")
            if self.candidate_decision == "RISK_NOT_CONFIRMED" and (
                not self.identified_security_mechanisms or not self.candidate_evidence
            ):
                raise ValueError(
                    "Rejected CF-005 candidate requires a concrete safeguard and Evidence"
                )
            if self.candidate_decision == "INSUFFICIENT_EVIDENCE" and self.status != "FAILED":
                raise ValueError("Insufficient CF-005 evidence must fail the required Check")
            if self.candidate_decision in {"TRIGGER_NOT_MET", "RISK_NOT_CONFIRMED"} and self.findings:
                raise ValueError("A rejected CF-005 risk cannot contain Findings")
        elif (
            self.candidate_decision is not None
            or bool(self.identified_security_mechanisms)
            or bool(self.candidate_evidence)
        ):
            raise ValueError("Candidate decision fields are reserved for CF-005")
        return self


class ModelCommercialReviewResponseRaw(StrictModel):
    check_results: list[ModelCheckCoverageResultRaw] = Field(min_length=1)


ReasonCode = Literal[
    "RISK_IDENTIFIED",
    "NO_RISK_IDENTIFIED",
    "NOT_APPLICABLE",
    "CHECK_FAILED",
    "INSUFFICIENT_EVIDENCE",
]


class ModelCheckCoverageResult(StrictModel):
    check_code: str = Field(pattern=r"^CF-[0-9]{3}$")
    status: Literal["REVIEWED", "NOT_APPLICABLE", "FAILED"]
    reason_code: ReasonCode
    decision_note: str = Field(min_length=1, max_length=1000)
    findings: list[ModelFindingDraft] = Field(default_factory=list)
    candidate_decision: Literal[
        "RISK_CONFIRMED",
        "RISK_NOT_CONFIRMED",
        "TRIGGER_NOT_MET",
        "INSUFFICIENT_EVIDENCE",
        "PERSPECTIVE_REJECTED",
    ] | None = None
    identified_security_mechanisms: list[str] = Field(default_factory=list, max_length=20)
    candidate_evidence: list[ModelEvidenceDraft] = Field(default_factory=list)


class ModelCommercialReviewResponse(StrictModel):
    check_results: list[ModelCheckCoverageResult] = Field(min_length=1)


class Cf005Candidate(StrictModel):
    check_code: Literal["CF-005"] = "CF-005"
    candidate_type: Literal["PREPAYMENT_WITHOUT_PERFORMANCE_SECURITY"] = (
        "PREPAYMENT_WITHOUT_PERFORMANCE_SECURITY"
    )
    substantial_prepayment: bool
    payment_before_performance: bool
    payer_role_status: Literal["OUR_PARTY", "COUNTERPARTY", "AMBIGUOUS"]
    installment_payment: bool
    milestone_linked: bool
    acceptance_linked: bool
    identified_security_mechanisms: list[str]
    candidate_ir_refs: list[str]
    candidate_evidence_refs: list[str]
    payment_ir_refs: list[str] = Field(default_factory=list)
    payment_evidence_refs: list[str] = Field(default_factory=list)
    trigger_absence_verified: bool = False
    requires_model_decision: Literal[True] = True


SchemaNormalizationType = Literal[
    "TOP_LEVEL_CHECKS_TO_CHECK_RESULTS",
    "CF005_EVIDENCE_REFS_TO_DRAFTS",
    "EMPTY_DECISION_NOTES_TO_STATUS_SUMMARY",
    "TOP_LEVEL_CHECKS_AND_CF005_EVIDENCE_REFS",
    "KNOWN_COMMERCIAL_SCHEMA_COMBINED",
    "KNOWN_GENERIC_SCHEMA_NORMALIZATION",
]


class SchemaNormalizationRecord(StrictModel):
    applied: bool
    normalization_type: SchemaNormalizationType | None = None

    @model_validator(mode="after")
    def validate_type(self) -> "SchemaNormalizationRecord":
        if self.applied != (self.normalization_type is not None):
            raise ValueError("Schema normalization type must match applied flag")
        return self


class ReasonCodeEnrichmentRecord(StrictModel):
    enrichment_count: int = Field(ge=0)
    ignored_model_reason_code_count: int = Field(ge=0)
    rule_version: Literal["1.0"] = "1.0"


class LlmAttemptDiagnostic(StrictModel):
    repair_no: int = Field(ge=0, le=1)
    raw_content: str
    raw_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_error: str | None = None
    schema_normalization_applied: bool = False
    schema_normalization_type: SchemaNormalizationType | None = None
    semantic_preservation_passed: bool | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    model_duration_ms: int = Field(ge=0)
    trace_id: str = Field(min_length=1, max_length=160)
    provider_request_id: str | None = Field(default=None, max_length=500)
    finish_reason: str | None = Field(default=None, max_length=160)
    validation_stage: str | None = None
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    normalized_output: dict[str, Any] | None = None
    changed_paths: list[str] = Field(default_factory=list)
    repair_check_codes: list[str] = Field(default_factory=list)


class EvidenceCandidate(StrictModel):
    evidence_local_id: str = Field(pattern=r"^evidence-[0-9a-f]{32}$")
    finding_local_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]
    source_ir_item_id: str | None = Field(default=None, max_length=160)
    anchor_id: str | None = Field(default=None, max_length=160)
    block_id: str | None = Field(default=None, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    quoted_text: str | None = None
    quoted_text_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    checked_scope: str | None = Field(default=None, min_length=1, max_length=500)
    verification_note: str | None = Field(default=None, min_length=1, max_length=2000)


class FindingDraft(StrictModel):
    finding_local_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    source_unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    domain: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
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
    risk_type: str = Field(min_length=1, max_length=160)
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    title: str = Field(min_length=1, max_length=300)
    issue: str = Field(min_length=1, max_length=2000)
    impact_to_our_party: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=2000)
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)
    evidence_candidates: list[EvidenceCandidate] = Field(min_length=1)
    legal_evidence_ids: list[str] = Field(default_factory=list)


class CheckCoverageResult(StrictModel):
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    status: Literal["REVIEWED", "NOT_APPLICABLE", "FAILED"]
    reason_code: ReasonCode
    decision_note: str = Field(min_length=1, max_length=1000)
    finding_local_ids: list[str] = Field(default_factory=list)


class LlmCallMetric(StrictModel):
    review_unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    repair_no: int = Field(ge=0, le=1)
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    model_duration_ms: int = Field(ge=0)
    trace_id: str = Field(min_length=1, max_length=160)
    provider_request_id: str | None = Field(default=None, max_length=500)
    finish_reason: str | None = Field(default=None, max_length=160)


class ReviewUnitResult(StrictModel):
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    domain: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    status: Literal["COMPLETED", "PARTIAL_FAILED"]
    check_results: list[CheckCoverageResult] = Field(min_length=1)
    findings: list[FindingDraft] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_call_count: int = Field(ge=1, le=2)
    repair_count: int = Field(ge=0, le=1)
    tool_call_count: Literal[0] = 0
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0)
    trace_ids: list[str] = Field(min_length=1, max_length=2)
    call_metrics: list[LlmCallMetric] = Field(min_length=1, max_length=2)
    repair_reasons: list[str] = Field(default_factory=list, max_length=1)
    schema_normalization_applied: bool = False
    schema_normalization_type: SchemaNormalizationType | None = None
    attempt_diagnostics: list[LlmAttemptDiagnostic] = Field(min_length=1, max_length=2)
    cf005_candidate: Cf005Candidate | None = None
    reason_code_enrichment_count: int = Field(ge=0)
    reason_code_rule_version: Literal["1.0"]
    ignored_model_reason_code_count: int = Field(ge=0)


class LlmCompleter(Protocol):
    async def complete_with_usage(
        self,
        messages: list[dict],
        model_id: str | None = None,
        system_prompt: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking_override: bool | None = None,
        response_format: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> LlmCompletionResult: ...


class DirectReviewError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        repairable: bool = False,
        structured_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.repairable = repairable
        self.structured_output = structured_output
        self.attempt_diagnostics: list[LlmAttemptDiagnostic] = []
        self.cf005_candidate: Cf005Candidate | None = None
        self.validation_stage: str = "BUSINESS"
        self.validation_issues: list[dict[str, Any]] = []
        self.normalized_output: dict[str, Any] | None = None
        self.check_codes: list[str] = []
        self.diagnostic_id: str | None = None


def enforce_provider_prompt_budget(
    completion: LlmCompletionResult,
    request: CommercialReviewRequest | Any,
) -> PromptBudgetResult:
    budget = evaluate_prompt_budget(
        unit_id=request.unit_id,
        batch_id=request.batch_id,
        estimated_business_context_tokens=(
            request.estimated_input_tokens + request.legal_evidence_input_tokens
        ),
        provider_prompt_tokens=completion.prompt_tokens,
        provider_cached_tokens=completion.cached_tokens,
    )
    return budget


@dataclass(frozen=True, slots=True)
class ParsedModelOutput:
    response: ModelCommercialReviewResponseRaw
    raw_object: dict[str, Any]
    normalization: SchemaNormalizationRecord
    normalized_object: dict[str, Any] | None = None


@dataclass(slots=True)
class CommercialFinancialDirectReviewer:
    runtime_factory: Callable[[str], LlmCompleter] = LlmRuntime

    async def review(
        self, request: CommercialReviewRequest, *, tenant_id: str, model_id: str,
        framework_run_id: str | None = None,
    ) -> ReviewUnitResult:
        identity = {"tenant_id": tenant_id, "review_id": request.review_id,
                    "generation_id": request.generation_id, "batch_id": request.batch_id,
                    "framework_run_id": framework_run_id, "model_id": model_id}
        try:
            result = await self._review(request, tenant_id=tenant_id, model_id=model_id,
                                        framework_run_id=framework_run_id)
        except (Exception, asyncio.CancelledError) as exc:
            exc.diagnostic_id = write_private_diagnostic("commercial_failed", identity, {
                "request": request.model_dump(mode="json"), "error_code": getattr(exc, "code", type(exc).__name__),
                "validation_stage": getattr(exc, "validation_stage", "PROVIDER_OR_RUNTIME"),
                "issues": getattr(exc, "validation_issues", []),
                "provider_failure": getattr(exc, "provider_failure", None),
                "attempts": [item.model_dump(mode="json") for item in getattr(exc, "attempt_diagnostics", [])],
            })
            raise
        write_private_diagnostic("commercial_completed", identity, {
            "request": request.model_dump(mode="json"), "result": result.model_dump(mode="json"),
        })
        return result

    async def _review(
        self,
        request: CommercialReviewRequest,
        *,
        tenant_id: str,
        model_id: str,
        framework_run_id: str | None = None,
    ) -> ReviewUnitResult:
        prompt, ir_refs, anchor_refs, cf005_candidate = _prompt(request)
        runtime = self.runtime_factory(tenant_id)
        started = time.perf_counter()
        calls: list[LlmCompletionResult] = []
        repair_reasons: list[str] = []
        invalid_content = ""
        invalid_reason = ""
        repair_baseline = None
        repair_error = None
        repair_targets: list[str] = []
        first_semantic_snapshot: dict[str, Any] | None = None
        diagnostics: list[LlmAttemptDiagnostic] = []
        accepted_normalization = SchemaNormalizationRecord(applied=False)
        accepted_enrichment = ReasonCodeEnrichmentRecord(
            enrichment_count=0,
            ignored_model_reason_code_count=0,
        )

        for repair_no in range(2):
            parsed: ParsedModelOutput | None = None
            messages: list[dict[str, str]] = [
                {"role": "user", "content": prompt}
            ]
            if repair_no:
                repair_payload, repair_targets = build_check_repair(
                    baseline=repair_baseline, error=repair_error,
                    request_context=json.loads(prompt[prompt.index("{"):]),
                    check_schema=ModelCheckCoverageResultRaw.model_json_schema(),
                )
                if not repair_targets:
                    # Malformed JSON has no identifiable check; at most this one
                    # financial batch can be repaired, never the entire review.
                    repair_payload["first_raw_json"] = invalid_content
                    repair_payload["exact_validation_error"] = invalid_reason
                # The repair payload already embeds the rejected JSON and exact
                # validation error. Re-sending the full business prompt and the
                # same rejected output as separate messages can more than double
                # provider input size and violate the hard prompt budget.
                messages = [
                    {
                        "role": "user",
                        "content": json.dumps(
                            repair_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    }
                ]
            try:
                completion = await runtime.complete_with_usage(
                    messages=messages,
                    model_id=model_id,
                    system_prompt=_SYSTEM_PROMPT,
                    max_tokens=4000,
                    temperature=0,
                    thinking_override=False,
                    response_format={"type": "json_object"},
                    review_unit_id=COMMERCIAL_UNIT_ID,
                    review_id=request.review_id,
                    framework_run_id=framework_run_id,
                    attempt_no=request.attempt_no,
                    repair_no=repair_no,
                    **deferred_completion_kwargs(
                        calls[-1] if calls else None,
                        repair_no=repair_no,
                    ),
                )
            except (Exception, asyncio.CancelledError) as exc:
                exc.attempt_diagnostics = diagnostics
                exc.provider_failure = {"repair_no": repair_no, "type": type(exc).__name__,
                                        "usage_status": "UNKNOWN_NO_COMPLETION_RECEIVED"}
                raise
            calls.append(completion)
            try:
                enforce_provider_prompt_budget(completion, request)
                content_to_parse = completion.content
                if repair_no and repair_targets:
                    try:
                        repaired = parse_json_output(completion.content)
                        merged = merge_check_repair(repair_baseline, repaired.structured if repaired.ok else None, repair_targets)
                    except (ValueError, TypeError) as exc:
                        raise DirectReviewError("RISK_REPAIR_SEMANTICS_CHANGED", str(exc)) from exc
                    content_to_parse = json.dumps(merged, ensure_ascii=False)
                parsed = _parse_model_output(
                    content_to_parse,
                    ir_refs=ir_refs,
                    anchor_refs=anchor_refs,
                    cf005_candidate=cf005_candidate,
                    assigned_check_specs=request.assigned_check_specs,
                )
                semantic_preservation_passed: bool | None = None
                if repair_no and first_semantic_snapshot is not None:
                    _validate_semantic_preservation(
                        first_semantic_snapshot,
                        _semantic_snapshot(parsed.normalized_object or parsed.raw_object),
                    )
                    semantic_preservation_passed = True
                enriched, enrichment = _enrich_reason_codes(parsed.response)
                result = _materialize(
                    request,
                    enriched,
                    ir_refs,
                    anchor_refs,
                    cf005_candidate,
                )
                if completion.completion_tokens is not None and completion.completion_tokens > 4000:
                    raise DirectReviewError("RISK_OUTPUT_BUDGET_EXCEEDED",
                                            "Commercial Direct Review exceeded the hard output token limit")
                diagnostics.append(
                    _attempt_diagnostic(
                        completion,
                        validation_error=None,
                        normalization=parsed.normalization,
                        semantic_preservation_passed=semantic_preservation_passed,
                        normalized_output=parsed.normalized_object,
                        repair_check_codes=repair_targets,
                    )
                )
                accepted_normalization = parsed.normalization
                accepted_enrichment = enrichment
                await finalize_completion_success(completion)
                break
            except DirectReviewError as exc:
                await finalize_completion_validation_failed(completion, exc.code)
                if not exc.check_codes:
                    exc.check_codes = sorted({"CF-" + item for item in re.findall(r"CF[-_](00[1-8])", exc.code + " " + str(exc))})
                if not exc.validation_issues:
                    exc.validation_stage = "EVIDENCE" if "EVIDENCE" in exc.code else "BUSINESS"
                    exc.validation_issues = validation_issues(exc, exc.validation_stage)
                if exc.normalized_output is None and parsed is not None:
                    exc.normalized_output = parsed.normalized_object
                diagnostics.append(
                    _attempt_diagnostic(
                        completion,
                        validation_error=f"{exc.code}: {exc}",
                        normalization=SchemaNormalizationRecord(applied=False),
                        semantic_preservation_passed=(
                            False if exc.code == "RISK_REPAIR_SEMANTICS_CHANGED" else None
                        ),
                        validation_stage=exc.validation_stage,
                        issues=exc.validation_issues,
                        normalized_output=exc.normalized_output,
                        repair_check_codes=repair_targets,
                    )
                )
                if not exc.repairable or repair_no == 1:
                    exc.attempt_diagnostics = diagnostics
                    exc.cf005_candidate = cf005_candidate
                    raise
                invalid_content = completion.content
                invalid_reason = f"{exc.code}: {exc}"
                repair_error = exc
                repair_baseline = exc.normalized_output or exc.structured_output
                first_semantic_snapshot = _semantic_snapshot(
                    repair_baseline
                )
                repair_reasons.append(invalid_reason)
            except asyncio.CancelledError as exc:
                await finalize_completion_validation_failed(
                    completion,
                    "MODEL_OUTPUT_PROCESSING_CANCELLED",
                )
                diagnostics.append(_attempt_diagnostic(completion, validation_error="MODEL_OUTPUT_PROCESSING_CANCELLED",
                    normalization=SchemaNormalizationRecord(applied=False), semantic_preservation_passed=None,
                    validation_stage="RUNTIME", normalized_output=parsed.normalized_object if parsed else None))
                exc.attempt_diagnostics = diagnostics
                raise
            except Exception as exc:
                await finalize_completion_validation_failed(
                    completion,
                    "MODEL_OUTPUT_PROCESSING_FAILED",
                )
                diagnostics.append(_attempt_diagnostic(completion, validation_error=type(exc).__name__,
                    normalization=SchemaNormalizationRecord(applied=False), semantic_preservation_passed=None,
                    validation_stage="RUNTIME", normalized_output=parsed.normalized_object if parsed else None))
                exc.attempt_diagnostics = diagnostics
                raise
        else:  # pragma: no cover - the loop either breaks or raises
            raise DirectReviewError("RISK_DIRECT_OUTPUT_INVALID", "Direct review did not return a result")

        duration_ms = round((time.perf_counter() - started) * 1000)
        metrics = [_metric(value) for value in calls]
        completion_tokens = _sum_optional(item.completion_tokens for item in calls)
        final_completion_tokens = calls[-1].completion_tokens
        if final_completion_tokens is not None and final_completion_tokens > 4000:
            raise DirectReviewError(
                "RISK_OUTPUT_BUDGET_EXCEEDED",
                "Commercial Direct Review exceeded the hard output token limit",
            )
        warnings = list(result[2])
        if request.legal_evidence_prompt_status == "OMITTED_TOKEN_BUDGET":
            warnings.append("LEGAL_EVIDENCE_OMITTED_TOKEN_BUDGET")
        if final_completion_tokens is not None and final_completion_tokens > 2500:
            warnings.append("RISK_OUTPUT_SOFT_LIMIT_EXCEEDED")
        return ReviewUnitResult(
            unit_id=COMMERCIAL_UNIT_ID,
            domain=COMMERCIAL_UNIT_ID,
            status=(
                "PARTIAL_FAILED"
                if any(
                    item.status == "FAILED"
                    or item.reason_code == "INSUFFICIENT_EVIDENCE"
                    for item in result[0]
                )
                else "COMPLETED"
            ),
            check_results=result[0],
            findings=result[1],
            warnings=warnings,
            model_call_count=len(calls),
            repair_count=len(calls) - 1,
            prompt_tokens=_sum_optional(item.prompt_tokens for item in calls),
            cached_tokens=_sum_optional(item.cached_tokens for item in calls),
            completion_tokens=completion_tokens,
            total_tokens=_sum_optional(item.total_tokens for item in calls),
            duration_ms=duration_ms,
            trace_ids=[item.trace_id for item in calls],
            call_metrics=metrics,
            repair_reasons=repair_reasons,
            schema_normalization_applied=accepted_normalization.applied,
            schema_normalization_type=accepted_normalization.normalization_type,
            attempt_diagnostics=diagnostics,
            cf005_candidate=cf005_candidate,
            reason_code_enrichment_count=accepted_enrichment.enrichment_count,
            reason_code_rule_version=accepted_enrichment.rule_version,
            ignored_model_reason_code_count=(
                accepted_enrichment.ignored_model_reason_code_count
            ),
        )


def commercial_request_from_context(
    value: BaseModel | dict[str, Any],
    *,
    legal_evidence: list[LegalEvidence] | None = None,
) -> CommercialReviewRequest:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    _, legal_tokens = compact_legal_evidence_catalog(
        legal_evidence or []
    )
    def project_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "ir_type": item["ir_type"],
            "item_id": item["item_id"],
            "subject": item.get("subject"),
            "predicate": item["predicate"],
            "object": item.get("object"),
            "source_anchors": [
                {"anchor_id": anchor["anchor_id"]}
                for anchor in item["source_anchors"]
            ],
        }

    return CommercialReviewRequest.model_validate(
        {
            "review_id": payload["review_id"],
            "document_id": payload["document_id"],
            "generation_id": payload["generation_id"],
            "attempt_no": payload["attempt_no"],
            "plan_id": payload["plan_id"],
            "context_hash": payload["context_hash"],
            "unit_id": payload["unit_id"],
            "batch_id": payload["batch_id"],
            "perspective": payload["perspective"],
            "our_party": payload["our_party"],
            "counterparty": payload["counterparty"],
            "contract_type": payload["contract_type"],
            "review_attitude": payload["review_attitude"],
            "assigned_check_specs": [
                {
                    "check_code": item["check_code"],
                    "review_question": item["review_question"],
                    "allowed_categories": item["allowed_categories"],
                    "allowed_risk_types": item["allowed_risk_types"],
                    "criticality": item["criticality"],
                }
                for item in payload["check_specs"]
            ],
            "definitions": [project_item(item) for item in payload["definitions"]],
            "projected_ir_items": [
                project_item(item) for item in payload["projected_ir_items"]
            ],
            "source_excerpts": payload["source_excerpts"],
            "check_task_scopes": payload.get("check_task_scopes", []),
            "estimated_input_tokens": payload["estimated_input_tokens"],
            "legal_evidence": legal_evidence or [],
            "legal_evidence_input_tokens": legal_tokens,
        }
    )


_CF005_RELEVANT_TYPES = {
    "payment_terms",
    "amounts",
    "dates",
    "delivery_terms",
    "acceptance_terms",
    "obligations",
    "liabilities",
    "termination_terms",
}
_CF005_PAYMENT_WORDS = ("支付", "付款", "价款", "费用", "预付")
_CF005_NON_PRICE_PAYMENT_WORDS = (
    "违约金",
    "赔偿",
    "罚款",
    "滞纳金",
    "利息",
    "损失",
)
_CF005_PREPAYMENT_WORDS = ("全额", "全部", "百分之百", "一次性", "绝大部分")
_CF005_BEFORE_PERFORMANCE_WORDS = (
    "签订后",
    "合同签订",
    "签订合同",
    "生效后",
    "合同生效",
    "收到发票",
    "开具发票",
    "预付",
)
_CF005_AFTER_PERFORMANCE_WORDS = (
    "交付后",
    "交付完成后",
    "履约完成后",
    "服务完成后",
    "验收后",
    "验收合格后",
)
_CF005_SECURITY_KEYWORDS = {
    "PERFORMANCE_GUARANTEE": ("履约保函", "预付款保函"),
    "PERFORMANCE_DEPOSIT": ("履约保证金",),
    "INSTALLMENT_PAYMENT": ("分期", "首期", "第二期", "尾款"),
    "MILESTONE_PAYMENT": ("里程碑", "节点付款", "阶段付款"),
    "ACCEPTANCE_LINKAGE": ("验收后", "验收合格后", "经甲方验收"),
    "REFUND_MECHANISM": ("退款", "返还", "退还"),
    "ESCROW": ("托管", "共管账户", "监管账户"),
    "GUARANTEE": ("担保", "保证人", "连带保证"),
}


def _cf005_is_price_payment(text: str) -> bool:
    """Keep breach payments out of the advance-payment payer calculation.

    CF-005 protects the party that pays contract consideration before the
    other side performs. A liquidated-damages payment is not consideration,
    even when its text happens to include a percentage of the contract price.
    """
    return bool(
        any(word in text for word in _CF005_PAYMENT_WORDS)
        and not any(word in text for word in _CF005_NON_PRICE_PAYMENT_WORDS)
    )


def _cf005_payer_role_status(
    statuses: list[str],
) -> Literal["OUR_PARTY", "COUNTERPARTY", "AMBIGUOUS"]:
    if "OUR_PARTY" in statuses:
        return "OUR_PARTY"
    if statuses and set(statuses) == {"COUNTERPARTY"}:
        return "COUNTERPARTY"
    return "AMBIGUOUS"


def _build_cf005_candidate(
    request: CommercialReviewRequest,
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> Cf005Candidate:
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    relevant: list[tuple[str, CommercialIrItem, str]] = []
    all_text_parts: list[str] = []
    for ref, item in ir_refs.items():
        if item.ir_type not in _CF005_RELEVANT_TYPES:
            continue
        text = "".join(
            value for value in (item.subject, item.predicate, item.object) if value
        )
        relevant.append((ref, item, text))
        all_text_parts.append(text)
    all_text = "\n".join(all_text_parts)

    substantial_refs: list[str] = []
    before_performance_refs: list[str] = []
    payer_status_by_ref: dict[str, str] = {}
    for ref, item, text in relevant:
        has_payment = _cf005_is_price_payment(text)
        payer_subject = item.subject or ""
        our_payer = text_names_role(
            payer_subject, roles.aliases_for_our_party()
        )
        counterparty_payer = text_names_role(
            payer_subject, roles.aliases_for_counterparty()
        )
        if our_payer and not counterparty_payer:
            payer_status = "OUR_PARTY"
        elif counterparty_payer and not our_payer:
            payer_status = "COUNTERPARTY"
        else:
            payer_status = "AMBIGUOUS"
        if has_payment:
            payer_status_by_ref[ref] = payer_status
        percentages = [
            float(value)
            for value in re.findall(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", text)
            if float(value) <= 100
        ]
        substantial = has_payment and (
            any(word in text for word in _CF005_PREPAYMENT_WORDS)
            or any(value >= 70 for value in percentages)
        )
        before_performance = has_payment and (
            any(word in text for word in _CF005_BEFORE_PERFORMANCE_WORDS)
            and not any(word in text for word in _CF005_AFTER_PERFORMANCE_WORDS)
        )
        if substantial:
            substantial_refs.append(ref)
        if before_performance:
            before_performance_refs.append(ref)

    mechanisms = [
        mechanism
        for mechanism, keywords in _CF005_SECURITY_KEYWORDS.items()
        if any(keyword in all_text for keyword in keywords)
    ]
    candidate_ir_refs = sorted(set(substantial_refs) | set(before_performance_refs))
    candidate_payer_statuses = [
        payer_status_by_ref[ref]
        for ref in candidate_ir_refs
        if ref in payer_status_by_ref
    ]
    candidate_evidence_refs = sorted(
        {
            anchor_ref_by_id[anchor.anchor_id]
            for ref in candidate_ir_refs
            for anchor in ir_refs[ref].source_anchors
        }
    )
    return Cf005Candidate(
        substantial_prepayment=bool(substantial_refs),
        payment_before_performance=bool(before_performance_refs),
        payer_role_status=_cf005_payer_role_status(candidate_payer_statuses),
        installment_payment="INSTALLMENT_PAYMENT" in mechanisms,
        milestone_linked="MILESTONE_PAYMENT" in mechanisms,
        acceptance_linked="ACCEPTANCE_LINKAGE" in mechanisms,
        identified_security_mechanisms=mechanisms,
        candidate_ir_refs=candidate_ir_refs,
        candidate_evidence_refs=candidate_evidence_refs,
        payment_ir_refs=sorted(payer_status_by_ref),
        payment_evidence_refs=sorted({anchor_ref_by_id[anchor.anchor_id]
                                     for ref in payer_status_by_ref for anchor in ir_refs[ref].source_anchors}),
        trigger_absence_verified=bool(payer_status_by_ref) and all(
            payer_status_by_ref[ref] != "AMBIGUOUS" and
            any(word in text for word in _CF005_AFTER_PERFORMANCE_WORDS)
            and not any(word in text for word in _CF005_BEFORE_PERFORMANCE_WORDS)
            for ref, _item, text in relevant if ref in payer_status_by_ref
        ),
    )


def _prompt(
    request: CommercialReviewRequest,
) -> tuple[
    str,
    dict[str, CommercialIrItem],
    dict[str, CommercialSourceExcerpt],
    Cf005Candidate,
]:
    excerpts = sorted(
        request.source_excerpts,
        key=lambda item: (item.block_no, item.char_start, item.anchor_id),
    )
    anchor_refs = {f"A{index:03d}": item for index, item in enumerate(excerpts, start=1)}
    anchor_ref_by_id = {item.anchor_id: ref for ref, item in anchor_refs.items()}
    ir_items = [*request.definitions, *request.projected_ir_items]
    ir_refs = {f"I{index:03d}": item for index, item in enumerate(ir_items, start=1)}
    projected: dict[str, list[dict[str, object]]] = {}
    for ref, item in ir_refs.items():
        projected.setdefault(item.ir_type, []).append(
            {
                "ir_ref": ref,
                "subject": item.subject,
                "predicate": item.predicate,
                "object": item.object,
                "evidence_refs": [
                    anchor_ref_by_id[anchor.anchor_id] for anchor in item.source_anchors
                ],
            }
        )
    cf005_candidate = _build_cf005_candidate(
        request, ir_refs, anchor_ref_by_id
    )
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    payload: dict[str, object] = {
        "review_context": {
            "perspective": request.perspective,
            "our_party": request.our_party,
            "counterparty": request.counterparty,
            "party_a": roles.party_a_name,
            "party_b": roles.party_b_name,
            "our_contract_role": roles.our_role,
            "counterparty_contract_role": roles.counterparty_role,
            "party_role_rule": (
                "甲方、乙方由上述party_a和party_b固定；不得根据付款方、"
                "履约方或参数顺序重新猜测，不得在Finding描述中倒置主体"
            ),
            "contract_type": request.contract_type,
            "review_attitude": request.review_attitude,
        },
        "assigned_check_specs": [
            {
                "check_code": item.check_code,
                "question": item.review_question,
                "category": item.allowed_categories[0],
                "risk_type": item.allowed_risk_types[0],
            }
            for item in request.assigned_check_specs
        ],
        "decision_policies": {
            code: {
                "object": policy.review_object,
                "triggers": policy.triggers,
                "non_risk": policy.non_risk_examples,
                "evidence": policy.minimum_evidence,
                "level": policy.risk_level_rule,
                "boundary": policy.boundary,
            }
            for code, policy in COMMERCIAL_DECISION_POLICIES.items()
            if code in {spec.check_code for spec in request.assigned_check_specs}
        },
        "cf005_candidate": cf005_candidate.model_dump(mode="json"),
        "check_task_records": [scope.prompt_record() for scope in request.check_task_scopes],
        "projected_ir": projected,
        "source_excerpts": [
            {
                "evidence_ref": ref,
                "text": item.quoted_text,
            }
            for ref, item in anchor_refs.items()
        ],
        "output_contract": {
            "required_check_codes": [item.check_code for item in request.assigned_check_specs],
            "check_status_enum": ["REVIEWED", "NOT_APPLICABLE", "FAILED"],
            "risk_level_enum": ["HIGH", "MEDIUM", "LOW", "INFO"],
            "rules": [
                "只回答本批required_check_codes，每个已分配问题记录一次，不补齐其他检查项、不凑数量",
                "每项只允许check_code,status,decision_note,findings,candidate_decision,identified_security_mechanisms,candidate_evidence",
                "candidate_ir、candidate_ir_refs及其他未列字段禁止输出",
                "reason_code由Python确定性生成，模型禁止输出",
                "无对我方不利的实质风险时：status=REVIEWED且findings=[]",
                "有Finding时status必须为REVIEWED，Finding的category/risk_type必须等于该Check允许值",
                "每个检查必须有非空decision_note，简述与原文对应的判断理由；无风险也不能省略或填写空字符串",
                "CF-005必须填写candidate_decision：RISK_CONFIRMED、RISK_NOT_CONFIRMED、TRIGGER_NOT_MET或INSUFFICIENT_EVIDENCE",
                "CF-005触发条件不成立（明确不是我方付款或无相关大额履约前付款）用TRIGGER_NOT_MET；不要求虚构保障措施；付款方不明不能当成不适用",
                "CF-005风险成立必须输出Finding；仅因已有保障排除风险时用RISK_NOT_CONFIRMED，并填写具体identified_security_mechanisms和candidate_evidence",
                "CF-005证据不足必须status=FAILED；其他Check省略三个candidate字段",
                "TEXT_QUOTE/CONTEXT必须同时提供同一IR允许的ir_ref和evidence_ref，另两个字段必须为null",
                "ABSENCE的ir_ref/evidence_ref必须为null，checked_scope/verification_note必须为非空字符串",
                "禁止输出本契约未列出的额外字段",
            ],
            "finding_required_fields": [
                "check_code",
                "category",
                "risk_type",
                "risk_level",
                "title",
                "issue",
                "impact_to_our_party",
                "suggestion",
                "evidence",
            ],
            "evidence_allowed_fields": [
                "evidence_type",
                "ir_ref",
                "evidence_ref",
                "checked_scope",
                "verification_note",
            ],
        },
    }
    baseline_prompt = (
        "审查Context。仅返回JSON；顶层只能是check_results；"
        "禁止checks、Markdown和JSON外的解释分析；JSON内decision_note是必填的简短判断依据。"
        "只记录本批实际分配的检查及其依据：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )
    legal_budget = remaining_legal_prompt_budget(
        baseline_prompt=baseline_prompt,
        system_prompt=_SYSTEM_PROMPT,
        hard_limit_tokens=PROVIDER_PROMPT_HARD_LIMIT_TOKENS,
    )
    had_legal_evidence = bool(request.legal_evidence)
    legal_catalog, legal_tokens = compact_legal_evidence_catalog(
        request.legal_evidence,
        maximum_catalog_tokens=legal_budget,
    )
    if legal_catalog:
        payload["legal_evidence_catalog"] = legal_catalog
        payload["legal_evidence_input_tokens"] = legal_tokens
        payload["output_contract"]["rules"].extend(
            [
                "legal_evidence_catalog仅作法律依据；不能代替合同原文Evidence",
                "UNVERIFIED的生效日期或状态不得表述为已核实现行有效",
                "法律Evidence ID由Python确定性绑定，模型禁止输出或编造",
            ]
        )
        request.legal_evidence_input_tokens = legal_tokens
        request.legal_evidence_prompt_status = "INCLUDED"
    else:
        if had_legal_evidence:
            request.legal_evidence_prompt_status = "OMITTED_TOKEN_BUDGET"
        # Keep the planner-owned Evidence objects for deterministic Check-to-ID
        # binding. Only their text catalog is omitted from the model prompt.
        request.legal_evidence_input_tokens = 0
    return (
        "审查Context。仅返回JSON；顶层只能是check_results；"
        "禁止checks、Markdown和JSON外的解释分析；JSON内decision_note是必填的简短判断依据。"
        "只记录本批实际分配的检查及其依据：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        ir_refs,
        anchor_refs,
        cf005_candidate,
    )


def _parse_model_output(
    content: str, *,
    ir_refs: dict[str, CommercialIrItem] | None = None,
    anchor_refs: dict[str, CommercialSourceExcerpt] | None = None,
    cf005_candidate: Cf005Candidate | None = None,
    assigned_check_specs: list[CommercialCheckSpec] | None = None,
) -> ParsedModelOutput:
    parsed = parse_json_output(content)
    if not parsed.ok or not isinstance(parsed.structured, dict):
        error = DirectReviewError("RISK_DIRECT_SCHEMA_INVALID",
                                  "Commercial Direct Review output is invalid: model did not return one complete JSON object",
                                  repairable=True)
        error.validation_stage = "JSON"
        error.validation_issues = validation_issues(ValueError("Expected one complete JSON object"), "JSON")
        raise error
    raw = parsed.structured
    initial_issues = []
    try:
        ModelCommercialReviewResponseRaw.model_validate(raw)
    except ValidationError as exc:
        initial_issues = validation_issues(exc, "INITIAL_SCHEMA")
    reference_issues: list[dict[str, Any]] = []
    result = _normalize_known_schema_error(
        raw, ir_refs=ir_refs, anchor_refs=anchor_refs, cf005_candidate=cf005_candidate,
        assigned_check_specs=assigned_check_specs, issues=reference_issues,
    )
    normalized, kind = result if result is not None else (copy.deepcopy(raw), None)
    current_issues = []
    try:
        response = ModelCommercialReviewResponseRaw.model_validate(normalized)
        # Field(min_length=1) alone accepts whitespace, which is still a missing explanation.
        for index, check in enumerate(response.check_results):
            if not check.decision_note.strip():
                current_issues.append({"stage": "SCHEMA", "path": ["check_results", index, "decision_note"],
                                       "type": "blank_explanation", "message": "decision_note must contain a nonblank explanation"})
    except ValidationError as exc:
        current_issues = validation_issues(exc, "SCHEMA")
    if result is not None and not current_issues:
        codes = [check.check_code for check in response.check_results]
        if len(codes) != len(set(codes)):
            current_issues.append({"stage": "SCHEMA", "path": ["check_results"],
                                   "type": "duplicate_check", "message": "Duplicate check codes cannot be normalized"})
    if current_issues or reference_issues:
        # Do not rethrow the initial error: preserve original AND post-normalization errors.
        issues = [*initial_issues, *current_issues, *reference_issues]
        stage = "SCHEMA" if current_issues else "EVIDENCE"
        code = "RISK_DIRECT_SCHEMA_INVALID"
        if not current_issues and reference_issues:
            first_message = reference_issues[0]["message"]
            code = ("RISK_EVIDENCE_IR_UNKNOWN" if first_message == "Unknown IR reference" else
                    "RISK_EVIDENCE_ANCHOR_UNKNOWN" if first_message == "Unknown contract anchor reference" else
                    "RISK_EVIDENCE_LINK_INVALID")
        error = DirectReviewError(code,
                                  "Commercial Direct Review output is invalid: " +
                                  json.dumps([*current_issues, *reference_issues], ensure_ascii=False),
                                  repairable=True, structured_output=raw)
        error.validation_stage = stage
        error.validation_issues = issues
        error.normalized_output = normalized
        error.check_codes = sorted({
            normalized["check_results"][item["path"][1]].get("check_code")
            for item in [*current_issues, *reference_issues]
            if len(item["path"]) > 1 and item["path"][0] == "check_results"
            and isinstance(item["path"][1], int)
            and isinstance(normalized.get("check_results"), list)
            and item["path"][1] < len(normalized["check_results"])
            and isinstance(normalized["check_results"][item["path"][1]], dict)
            and re.fullmatch(r"CF-[0-9]{3}", str(normalized["check_results"][item["path"][1]].get("check_code", "")))
        })
        raise error
    return ParsedModelOutput(response=response, raw_object=raw,
                             normalization=SchemaNormalizationRecord(applied=kind is not None, normalization_type=kind),
                             normalized_object=normalized)


def _normalize_known_schema_error(
    value: dict[str, Any],
    *,
    ir_refs: dict[str, CommercialIrItem] | None = None,
    anchor_refs: dict[str, CommercialSourceExcerpt] | None = None,
    cf005_candidate: Cf005Candidate | None = None,
    assigned_check_specs: list[CommercialCheckSpec] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], SchemaNormalizationType] | None:
    """Format and exact-reference normalization, never create a risk or a verdict note.

    Each failed reference is retained AND diagnosed. A missing note cannot hide
    an independent evidence error. Business conditions run separately in materialize.
    """
    issues = issues if issues is not None else []
    normalized = copy.deepcopy(value)
    top_changed = False
    if set(normalized) == {"checks"} and isinstance(normalized["checks"], list):
        normalized["check_results"] = normalized.pop("checks")
        top_changed = True
    if set(normalized) == {"check_results", "summary"} and isinstance(normalized["summary"], str):
        normalized.pop("summary")
        top_changed = True
    checks = normalized.get("check_results")
    if not isinstance(checks, list):
        return None
    evidence_changed = False
    fields_changed = False
    specs = {item.check_code: item for item in assigned_check_specs or []}
    ir_refs, anchor_refs = ir_refs or {}, anchor_refs or {}

    def issue(path, message):
        issues.append({"stage": "EVIDENCE", "path": path,
                       "type": "reference_binding", "message": message})

    def bind(entry, path):
        nonlocal evidence_changed
        if isinstance(entry, dict) and entry.get("evidence_type") == "ABSENCE":
            return entry
        explicit = isinstance(entry, dict) and bool(entry.get("evidence_type"))
        if isinstance(entry, str):
            match = re.match(r"^(?P<ref>[IA][0-9]{3})(?:$|[\\s:：,，。;；]|条款)", entry)
            if not match:
                issue(path, "Evidence shorthand is not an exact source reference")
                return entry
            ref = match.group("ref")
            item = {"ir_ref" if ref.startswith("I") else "evidence_ref": ref}
        elif isinstance(entry, dict):
            item = dict(entry)
            if not explicit and set(item) - {"ir_ref", "evidence_ref", "checked_scope", "verification_note"}:
                issue(path, "Unknown evidence fields cannot be silently discarded")
                return entry
        else:
            issue(path, "Evidence must be an object or an exact source reference")
            return entry
        ir_ref, anchor_ref = item.get("ir_ref"), item.get("evidence_ref")
        # Without maps (schema-only callers), keep explicit objects for later binding.
        if explicit and not ir_refs and not anchor_refs:
            return entry
        if ir_ref is not None and (not isinstance(ir_ref, str) or ir_ref not in ir_refs):
            issue(path, "Unknown IR reference")
            return entry
        if anchor_ref is not None and (not isinstance(anchor_ref, str) or anchor_ref not in anchor_refs):
            issue(path, "Unknown contract anchor reference")
            return entry
        if ir_ref and not anchor_ref:
            anchor_ref = next((ref for ref, anchor in anchor_refs.items()
                               if anchor.anchor_id in {a.anchor_id for a in ir_refs[ir_ref].source_anchors}), None)
        if anchor_ref and not ir_ref:
            ir_ref = next((ref for ref in sorted(ir_refs)
                           if anchor_refs[anchor_ref].anchor_id in {a.anchor_id for a in ir_refs[ref].source_anchors}), None)
        if not ir_ref or not anchor_ref or anchor_refs[anchor_ref].anchor_id not in {
            a.anchor_id for a in ir_refs[ir_ref].source_anchors
        }:
            issue(path, "Evidence does not bind to an IR fact and its original contract anchor")
            return entry
        if explicit:
            return entry
        result = {"evidence_type": "TEXT_QUOTE", "ir_ref": ir_ref, "evidence_ref": anchor_ref}
        evidence_changed = True
        return result

    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        code = check.get("check_code")
        spec = specs.get(code) if isinstance(code, str) else None
        category, risk_type = check.get("category"), check.get("risk_type")
        if spec:
            for key, allowed in (("category", spec.allowed_categories), ("risk_type", spec.allowed_risk_types)):
                if key in check and check[key] in allowed:
                    check.pop(key)
                    fields_changed = True
        findings = check.get("findings", [])
        if isinstance(findings, dict):
            findings = [findings]
            check["findings"] = findings
            fields_changed = True
        if code == "CF-005":
            decision = check.get("candidate_decision")
            if decision is None:
                if isinstance(findings, list) and findings:
                    decision = "RISK_CONFIRMED"
                elif check.get("status") == "FAILED":
                    decision = "INSUFFICIENT_EVIDENCE"
                elif cf005_candidate is not None and _cf005_trigger_not_met(cf005_candidate):
                    decision = "TRIGGER_NOT_MET"
                elif cf005_candidate is not None and cf005_candidate.identified_security_mechanisms:
                    decision = "RISK_NOT_CONFIRMED"
                # Never manufacture a Finding from a missing model decision.
            if (decision == "RISK_NOT_CONFIRMED" and cf005_candidate is not None
                    and _cf005_trigger_not_met(cf005_candidate)
                    and not check.get("identified_security_mechanisms")):
                decision = "TRIGGER_NOT_MET"
            if decision is not None and check.get("candidate_decision") != decision:
                check["candidate_decision"] = decision
                fields_changed = True
            mechanisms = check.get("identified_security_mechanisms")
            if mechanisms is None and decision is not None:
                mechanisms = (list(cf005_candidate.identified_security_mechanisms)
                              if decision == "RISK_NOT_CONFIRMED" and cf005_candidate is not None else [])
                check["identified_security_mechanisms"] = mechanisms
                fields_changed = True
            elif isinstance(mechanisms, str) and cf005_candidate is not None and mechanisms in cf005_candidate.identified_security_mechanisms:
                mechanisms = [mechanisms]
                check["identified_security_mechanisms"] = mechanisms
                fields_changed = True
            evidence = check.get("candidate_evidence")
            if isinstance(evidence, dict):
                evidence = [evidence]
            elif isinstance(evidence, str) and re.match(r"^[IA][0-9]{3}(?:$|[\\s:：,，。;；]|条款)", evidence):
                evidence = [evidence]
            elif (evidence is None or isinstance(evidence, str) or evidence == []) and decision == "RISK_NOT_CONFIRMED":
                # Recover safeguards only from text containing the named, known mechanism.
                refs = [ref for ref, anchor in anchor_refs.items()
                        if any(word in anchor.quoted_text
                               for mechanism in mechanisms or []
                               for word in _CF005_SECURITY_KEYWORDS.get(mechanism, ()))]
                if refs:
                    evidence = refs
            elif evidence is None and decision in {"TRIGGER_NOT_MET", "INSUFFICIENT_EVIDENCE", "RISK_CONFIRMED"}:
                evidence = []
            if isinstance(evidence, list):
                evidence = [bind(entry, ["check_results", index, "candidate_evidence", i])
                            for i, entry in enumerate(evidence)]
                if check.get("candidate_evidence") != evidence:
                    check["candidate_evidence"] = evidence
                    evidence_changed = True
        for finding_index, finding in enumerate(findings if isinstance(findings, list) else []):
            if not isinstance(finding, dict):
                continue
            if spec:
                for key, redundant, allowed in (("category", category, spec.allowed_categories),
                                                 ("risk_type", risk_type, spec.allowed_risk_types)):
                    if finding.get(key) is None and (len(allowed) == 1 or redundant in allowed):
                        finding[key] = redundant if redundant in allowed else allowed[0]
                        fields_changed = True
            evidence = finding.get("evidence")
            if isinstance(evidence, dict):
                evidence = [evidence]
                evidence_changed = True
            if isinstance(evidence, list):
                finding["evidence"] = [bind(entry, ["check_results", index, "findings", finding_index, "evidence", i])
                                      for i, entry in enumerate(evidence)]
    if normalized == value:
        return None
    if top_changed and evidence_changed and not fields_changed:
        kind = "TOP_LEVEL_CHECKS_AND_CF005_EVIDENCE_REFS"
    elif sum((top_changed, evidence_changed, fields_changed)) > 1 or fields_changed:
        kind = "KNOWN_COMMERCIAL_SCHEMA_COMBINED"
    elif top_changed:
        kind = "TOP_LEVEL_CHECKS_TO_CHECK_RESULTS"
    else:
        kind = "CF005_EVIDENCE_REFS_TO_DRAFTS"
    return normalized, kind


def _cf005_trigger_not_met(candidate: Cf005Candidate) -> bool:
    # Unknown payer is NOT evidence of non-applicability.
    return (candidate.payer_role_status == "COUNTERPARTY" and bool(candidate.candidate_evidence_refs)) or (
        candidate.trigger_absence_verified and bool(candidate.payment_ir_refs) and bool(candidate.payment_evidence_refs))


def _enrich_reason_codes(
    value: ModelCommercialReviewResponseRaw,
) -> tuple[ModelCommercialReviewResponse, ReasonCodeEnrichmentRecord]:
    enriched_checks: list[dict[str, Any]] = []
    ignored_count = 0
    for check in value.check_results:
        if "reason_code" in check.model_fields_set:
            ignored_count += 1
        if check.status == "REVIEWED":
            reason_code: ReasonCode = (
                "RISK_IDENTIFIED" if check.findings else "NO_RISK_IDENTIFIED"
            )
        elif (
            check.status == "FAILED"
            and check.candidate_decision == "INSUFFICIENT_EVIDENCE"
        ):
            reason_code = "INSUFFICIENT_EVIDENCE"
        elif check.status == "NOT_APPLICABLE":
            reason_code = "NOT_APPLICABLE"
        else:
            reason_code = "CHECK_FAILED"
        enriched_checks.append(
            {
                "check_code": check.check_code,
                "status": check.status,
                "reason_code": reason_code,
                "decision_note": check.decision_note,
                "findings": [
                    item.model_dump(mode="json") for item in check.findings
                ],
                "candidate_decision": check.candidate_decision,
                "identified_security_mechanisms": (
                    check.identified_security_mechanisms or []
                ),
                "candidate_evidence": [
                    item.model_dump(mode="json")
                    for item in (check.candidate_evidence or [])
                ],
            }
        )
    final = ModelCommercialReviewResponse.model_validate(
        {"check_results": enriched_checks}
    )
    return final, ReasonCodeEnrichmentRecord(
        enrichment_count=len(enriched_checks),
        ignored_model_reason_code_count=ignored_count,
    )


def _semantic_snapshot(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    checks = value.get("check_results")
    if checks is None:
        checks = value.get("checks")
    if not isinstance(checks, list):
        return None
    snapshot: dict[str, Any] = {}
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("check_code"), str):
            return None
        check_code = item["check_code"]
        if check_code in snapshot or not isinstance(item.get("findings", []), list):
            return None
        protected_findings = []
        for finding in item.get("findings", []):
            if not isinstance(finding, dict):
                return None
            protected_findings.append(
                {
                    key: finding.get(key)
                    for key in (
                        "check_code",
                        "category",
                        "risk_type",
                        "risk_level",
                        "title",
                        "issue",
                        "impact_to_our_party",
                        "suggestion",
                        "evidence",
                    )
                }
            )
        snapshot[check_code] = {
            # A repair may correct a nonblank explanation. The original and
            # repaired text remain in attempt diagnostics; only the verdict
            # and findings stay frozen by this guard. Untargeted checks are
            # separately protected by merge_check_repair.
            "protected_check_fields": {
                key: item[key]
                for key in (
                    "status",
                    "candidate_decision",
                    "identified_security_mechanisms",
                    "candidate_evidence",
                )
                if key in item
            },
            "findings": protected_findings,
        }
    return snapshot


def _validate_semantic_preservation(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    if before is None or after is None:
        raise DirectReviewError(
            "RISK_REPAIR_SEMANTICS_UNVERIFIABLE",
            "Repair output cannot be accepted because semantic preservation is unverifiable",
        )
    if set(before) != set(after):
        raise DirectReviewError(
            "RISK_REPAIR_SEMANTICS_CHANGED",
            "Repair changed the set of check codes",
        )
    for check_code in sorted(before):
        previous = before[check_code]
        current = after[check_code]
        for field, previous_value in previous["protected_check_fields"].items():
            if current["protected_check_fields"].get(field) != previous_value:
                raise DirectReviewError(
                    "RISK_REPAIR_SEMANTICS_CHANGED",
                    f"Repair changed {check_code}.{field}",
                )
        if previous["findings"] != current["findings"]:
            raise DirectReviewError(
                "RISK_REPAIR_SEMANTICS_CHANGED",
                f"Repair added, removed, reassigned, or changed Findings/Evidence for {check_code}",
            )


def _attempt_diagnostic(
    value: LlmCompletionResult,
    *,
    validation_error: str | None,
    normalization: SchemaNormalizationRecord,
    semantic_preservation_passed: bool | None,
    validation_stage: str | None = None,
    issues: list[dict[str, Any]] | None = None,
    normalized_output: dict[str, Any] | None = None,
    repair_check_codes: list[str] | None = None,
) -> LlmAttemptDiagnostic:
    return LlmAttemptDiagnostic(
        repair_no=value.repair_no,
        raw_content=value.content,
        raw_content_sha256="sha256:" + hashlib.sha256(value.content.encode("utf-8")).hexdigest(),
        validation_error=validation_error,
        schema_normalization_applied=normalization.applied,
        schema_normalization_type=normalization.normalization_type,
        semantic_preservation_passed=semantic_preservation_passed,
        prompt_tokens=value.prompt_tokens,
        cached_tokens=value.cached_tokens,
        completion_tokens=value.completion_tokens,
        total_tokens=value.total_tokens,
        time_to_first_token_ms=value.time_to_first_token_ms,
        model_duration_ms=value.model_duration_ms,
        trace_id=value.trace_id,
        provider_request_id=value.provider_request_id,
        finish_reason=value.finish_reason,
        validation_stage=validation_stage,
        validation_issues=issues or [],
        normalized_output=normalized_output,
        changed_paths=structural_changes(parse_json_output(value.content).structured, normalized_output)
            if normalized_output is not None else [],
        repair_check_codes=repair_check_codes or [],
    )


def _materialize(
    request: CommercialReviewRequest,
    response: ModelCommercialReviewResponse,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
    cf005_candidate: Cf005Candidate,
) -> tuple[list[CheckCoverageResult], list[FindingDraft], list[str]]:
    by_code: dict[str, ModelCheckCoverageResult] = {}
    for check in response.check_results:
        if check.check_code in by_code:
            raise DirectReviewError(
                "RISK_CHECK_DUPLICATED",
                f"Duplicate check result: {check.check_code}",
                repairable=True,
            )
        by_code[check.check_code] = check
    assigned_codes = [item.check_code for item in request.assigned_check_specs]
    if set(by_code) != set(assigned_codes):
        raise DirectReviewError(
            "RISK_CHECK_COVERAGE_INVALID",
            "Commercial Direct Review must record each assigned Check exactly once",
            repairable=True,
        )
    cf005_warning = None
    partial_codes = {scope.check_code for scope in request.check_task_scopes if not scope.complete}
    for code in partial_codes & by_code.keys():
        check = by_code[code]
        if check.findings:
            if any(e.evidence_type == "ABSENCE" for f in check.findings for e in f.evidence):
                raise DirectReviewError("RISK_PARTIAL_SCOPE_ABSENCE_INVALID",
                                        "A partial evidence scope cannot prove global absence", repairable=True)
        else:
            # Preserve the observation but do not certify its global negative.
            by_code[code] = check.model_copy(update={
                "status": "REVIEWED", "reason_code": "INSUFFICIENT_EVIDENCE",
                "decision_note": "[局部证据，尚不能作全局结论] " + check.decision_note[:950],
                **({"candidate_decision": "INSUFFICIENT_EVIDENCE"} if code == "CF-005" else {}),
            })
    if "CF-005" in by_code:
        cf005_check, cf005_warning = _suppress_cf005_perspective_conflict(
            by_code["CF-005"], cf005_candidate,
        )
        cf005_check = _enrich_cf005_confirmed_absence(cf005_check, cf005_candidate)
        by_code["CF-005"] = cf005_check
        _validate_cf005_candidate_decision(cf005_check, cf005_candidate, ir_refs, anchor_refs)
    specs = {item.check_code: item for item in request.assigned_check_specs}
    findings: list[FindingDraft] = []
    coverage: list[CheckCoverageResult] = []
    for check_code in assigned_codes:
        check = by_code[check_code]
        local_ids: list[str] = []
        for model_finding in check.findings:
            if model_finding.check_code != check_code:
                raise DirectReviewError(
                    "RISK_FINDING_CHECK_INVALID",
                    "Finding references a different or unknown check",
                    repairable=True,
                )
            spec = specs[check_code]
            if (
                model_finding.category not in spec.allowed_categories
                or model_finding.risk_type not in spec.allowed_risk_types
            ):
                raise DirectReviewError(
                    "RISK_FINDING_TYPE_INVALID",
                    "Finding category or risk_type is not allowed by its CheckSpec",
                    repairable=True,
                )
            finding = _finding(request, model_finding, ir_refs, anchor_refs)
            _validate_check_evidence(check_code, finding)
            local_ids.append(finding.finding_local_id)
            findings.append(finding)
        if check.findings and check.status != "REVIEWED":
            raise DirectReviewError(
                "RISK_CHECK_STATUS_INVALID",
                "A check with Findings must be REVIEWED",
                repairable=True,
            )
        coverage.append(
            CheckCoverageResult(
                check_code=check_code,
                status=check.status,
                reason_code=check.reason_code,
                decision_note=check.decision_note,
                finding_local_ids=local_ids,
            )
        )
    finding_ids = [item.finding_local_id for item in findings]
    evidence_ids = [
        evidence.evidence_local_id
        for finding in findings
        for evidence in finding.evidence_candidates
    ]
    if len(finding_ids) != len(set(finding_ids)) or len(evidence_ids) != len(set(evidence_ids)):
        raise DirectReviewError(
            "RISK_FINDING_DUPLICATED",
            "Commercial Direct Review returned duplicate material Findings or Evidence",
            repairable=True,
        )
    return (
        coverage,
        findings,
        [cf005_warning] if cf005_warning is not None else [],
    )


def _enrich_cf005_confirmed_absence(
    check: ModelCheckCoverageResult,
    candidate: Cf005Candidate,
) -> ModelCheckCoverageResult:
    """Attach the deterministic missing-safeguard fact to a confirmed risk.

    The candidate builder has already scanned the exact payment context for
    the frozen safeguard catalogue. Adding this ABSENCE record does not infer
    a new risk or alter the model verdict; it makes the Python gate that
    justified the confirmed CF-005 decision traceable on every Finding.
    """

    if (
        check.candidate_decision != "RISK_CONFIRMED"
        or not check.findings
    ):
        return check
    enriched_findings: list[ModelFindingDraft] = []
    for finding in check.findings:
        evidence = list(finding.evidence)
        if (
            any(
                item.evidence_type in {"TEXT_QUOTE", "CONTEXT"}
                for item in evidence
            )
            and not any(item.evidence_type == "ABSENCE" for item in evidence)
        ):
            evidence.append(
                ModelEvidenceDraft(
                    evidence_type="ABSENCE",
                    checked_scope=(
                        "当前CF-005付款条款及其关联履约保障、分期、里程碑和验收机制"
                    ),
                    verification_note=(
                        (
                            "Python确定性候选扫描未识别履约保函、保证金、托管、"
                            "分期、里程碑或验收挂钩等保障机制；仅说明当前合同文本。"
                        )
                        if not candidate.identified_security_mechanisms
                        else (
                            "模型在已识别保障机制条件下仍确认CF-005风险；本记录仅表示"
                            "其未确认存在足以消除该预付风险的保障组合，不扩展原判定。"
                        )
                    ),
                )
            )
        enriched_findings.append(
            finding.model_copy(update={"evidence": evidence})
        )
    return check.model_copy(update={"findings": enriched_findings})


def _suppress_cf005_perspective_conflict(
    check: ModelCheckCoverageResult,
    candidate: Cf005Candidate,
) -> tuple[ModelCheckCoverageResult, str | None]:
    """Reject only a reversed CF-005 Finding, without failing the review unit.

    A CF-005 advance-payment risk is valid only when the fixed review party is
    the payer of the identified consideration. If the source says the
    counterparty pays, or the payer cannot be determined, the model's Finding
    is excluded before revision-draft generation. Other commercial checks keep
    running normally.
    """
    if not check.findings or candidate.payer_role_status == "OUR_PARTY":
        return check, None
    if candidate.payer_role_status == "AMBIGUOUS":
        return (check.model_copy(update={
            "status": "FAILED", "reason_code": "INSUFFICIENT_EVIDENCE",
            "decision_note": "付款方无法由当前合同事实确定，不能确认对我方的预付款风险，需补充核验。",
            "findings": [], "candidate_decision": "INSUFFICIENT_EVIDENCE",
        }), "RISK_CF005_PAYER_UNRESOLVED")
    payer_description = (
        "合同价款付款方是相对方"
        if candidate.payer_role_status == "COUNTERPARTY"
        else "合同价款付款方无法由原文确定"
    )
    return (
        check.model_copy(
            update={
                "status": "REVIEWED",
                "reason_code": "NO_RISK_IDENTIFIED",
                "decision_note": (
                    "RISK_PERSPECTIVE_CONFLICT："
                    f"{payer_description}，已排除反向预付款风险，不进入修订草案"
                ),
                "findings": [],
                "candidate_decision": "PERSPECTIVE_REJECTED",
            }
        ),
        "RISK_PERSPECTIVE_CONFLICT",
    )


def _validate_cf005_candidate_decision(
    check: ModelCheckCoverageResult,
    candidate: Cf005Candidate,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> None:
    if check.candidate_decision == "PERSPECTIVE_REJECTED":
        return
    if check.candidate_decision == "INSUFFICIENT_EVIDENCE":
        # Remains FAILED and the complete-review gate still blocks final success.
        return
    if check.candidate_decision == "TRIGGER_NOT_MET":
        if not _cf005_trigger_not_met(candidate):
            raise DirectReviewError(
                "RISK_CF005_APPLICABILITY_INVALID",
                "CF-005 trigger rejection requires verified payer or payment-condition facts; unknown is not inapplicable",
                repairable=True,
            )
        return
    if check.findings and candidate.payer_role_status != "OUR_PARTY":
        raise DirectReviewError(
            "RISK_CF005_PAYER_PERSPECTIVE_INVALID",
            "CF-005 Finding requires deterministic evidence that our_party is the payer",
            repairable=True,
        )
    strong_unsecured_candidate = (
        candidate.substantial_prepayment
        and candidate.payment_before_performance
        and candidate.payer_role_status == "OUR_PARTY"
        and not candidate.identified_security_mechanisms
    )
    if strong_unsecured_candidate and check.candidate_decision != "RISK_CONFIRMED":
        raise DirectReviewError(
            "RISK_CF005_CANDIDATE_SKIPPED",
            "CF-005 cannot reject or skip a substantial pre-performance payment "
            "candidate without any identified safeguard",
            repairable=True,
        )
    if check.candidate_decision == "RISK_NOT_CONFIRMED":
        if not check.identified_security_mechanisms or not check.candidate_evidence:
            raise DirectReviewError("RISK_CF005_SAFEGUARD_EVIDENCE_INVALID",
                                    "CF-005 safeguard rejection requires a named safeguard and its evidence", repairable=True)
        for index, evidence in enumerate(check.candidate_evidence, start=1):
            if evidence.evidence_type == "ABSENCE":
                raise DirectReviewError(
                    "RISK_CF005_SAFEGUARD_EVIDENCE_INVALID",
                    "A safeguard that rejects CF-005 must use source text Evidence",
                    repairable=True,
                )
            _evidence_candidate(
                "finding-" + "0" * 32,
                index,
                evidence,
                ir_refs,
                anchor_refs,
            )
        # A semantic IR can describe installment payments while the original text
        # says first/second delivery followed by 25% payments. Do not introduce a
        # new lexical business rule requiring the literal word 'installment'.
        # The known mechanism catalogue and all exact source bindings still apply.
        if any(mechanism not in candidate.identified_security_mechanisms
               for mechanism in check.identified_security_mechanisms):
            raise DirectReviewError("RISK_CF005_SAFEGUARD_EVIDENCE_INVALID",
                                    "CF-005 named safeguard is outside the source-backed candidate catalogue", repairable=True)


def _validate_check_evidence(check_code: str, finding: FindingDraft) -> None:
    evidence_types = {item.evidence_type for item in finding.evidence_candidates}
    text_types = {"TEXT_QUOTE", "CONTEXT"}
    if check_code == "CF-004" and evidence_types.isdisjoint(text_types):
        raise DirectReviewError(
            "RISK_CF004_EVIDENCE_INVALID",
            "CF-004 requires source text for the adjustment, deduction, set-off, or settlement risk",
            repairable=True,
        )
    if check_code == "CF-005" and (
        evidence_types.isdisjoint(text_types) or "ABSENCE" not in evidence_types
    ):
        raise DirectReviewError(
            "RISK_CF005_EVIDENCE_INVALID",
            "CF-005 advance-payment risk requires payment source text and an ABSENCE safeguard check",
            repairable=True,
        )


def _finding(
    request: CommercialReviewRequest,
    value: ModelFindingDraft,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> FindingDraft:
    source_keys = [
        f"{item.ir_ref or '-'}:{item.evidence_ref or '-'}:{item.evidence_type}"
        for item in value.evidence
    ]
    finding_id = _stable_id(
        "finding",
        {
            "plan_id": request.plan_id,
            "unit_id": request.unit_id,
            "check_code": value.check_code,
            "category": value.category,
            "risk_type": value.risk_type,
            "sources": source_keys,
        },
    )
    evidence = [
        _evidence_candidate(finding_id, index, draft, ir_refs, anchor_refs)
        for index, draft in enumerate(value.evidence, start=1)
    ]
    return FindingDraft(
        finding_local_id=finding_id,
        source_unit_id=COMMERCIAL_UNIT_ID,
        domain=COMMERCIAL_UNIT_ID,
        check_code=value.check_code,
        category=value.category,
        risk_type=value.risk_type,
        risk_level=value.risk_level,
        title=value.title,
        issue=value.issue,
        impact_to_our_party=value.impact_to_our_party,
        suggestion=value.suggestion,
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
        evidence_candidates=evidence,
        legal_evidence_ids=legal_evidence_ids_for_check(
            request.legal_evidence,
            value.check_code,
        ),
    )


def _evidence_candidate(
    finding_id: str,
    index: int,
    value: ModelEvidenceDraft,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> EvidenceCandidate:
    evidence_id = _stable_id(
        "evidence",
        {
            "finding_id": finding_id,
            "index": index,
            "evidence_type": value.evidence_type,
            "ir_ref": value.ir_ref,
            "evidence_ref": value.evidence_ref,
        },
    )
    if value.evidence_type == "ABSENCE":
        return EvidenceCandidate(
            evidence_local_id=evidence_id,
            finding_local_id=finding_id,
            evidence_type="ABSENCE",
            checked_scope=value.checked_scope,
            verification_note=value.verification_note,
        )
    item = ir_refs.get(value.ir_ref or "")
    excerpt = anchor_refs.get(value.evidence_ref or "")
    if item is None:
        raise DirectReviewError(
            "RISK_EVIDENCE_IR_UNKNOWN",
            "Evidence references an unknown IR item",
            repairable=True,
        )
    if excerpt is None:
        raise DirectReviewError(
            "RISK_EVIDENCE_ANCHOR_UNKNOWN",
            "Evidence references an unknown Anchor",
            repairable=True,
        )
    if excerpt.anchor_id not in {anchor.anchor_id for anchor in item.source_anchors}:
        raise DirectReviewError(
            "RISK_EVIDENCE_LINK_INVALID",
            "Evidence Anchor does not belong to the referenced IR item",
            repairable=True,
        )
    return EvidenceCandidate(
        evidence_local_id=evidence_id,
        finding_local_id=finding_id,
        evidence_type=value.evidence_type,
        source_ir_item_id=item.item_id,
        anchor_id=excerpt.anchor_id,
        block_id=excerpt.block_id,
        page_number=excerpt.page_number,
        char_start=excerpt.char_start,
        char_end=excerpt.char_end,
        quoted_text=excerpt.quoted_text,
        quoted_text_hash=excerpt.quoted_text_hash,
    )


def _metric(value: LlmCompletionResult) -> LlmCallMetric:
    if value.review_unit_id != COMMERCIAL_UNIT_ID:
        raise DirectReviewError(
            "RISK_USAGE_ATTRIBUTION_INVALID",
            "LLM usage is not attributed to commercial_financial",
        )
    return LlmCallMetric(
        review_unit_id=COMMERCIAL_UNIT_ID,
        repair_no=value.repair_no,
        prompt_tokens=value.prompt_tokens,
        cached_tokens=value.cached_tokens,
        completion_tokens=value.completion_tokens,
        total_tokens=value.total_tokens,
        time_to_first_token_ms=value.time_to_first_token_ms,
        model_duration_ms=value.model_duration_ms,
        trace_id=value.trace_id,
        provider_request_id=value.provider_request_id,
        finish_reason=value.finish_reason,
    )


def _sum_optional(values) -> int | None:
    items = list(values)
    return sum(items) if all(item is not None for item in items) else None


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:32]}"
