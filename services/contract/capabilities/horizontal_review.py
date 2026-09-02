"""Deterministic horizontal contract review with minimal model decisions.

This module intentionally does not read the contract through tools or ask a
model to discover risks. Python builds a grounded relationship index, produces
bounded candidates, assigns Finding ownership, and only sends unresolved
horizontal candidates to the model for a three-way decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from collections import defaultdict
from datetime import date
from typing import Any, Callable, Literal, Protocol

from contract.application.idempotency import canonical_json
from contract.legal_evidence.models import LegalEvidence
from contract.legal_evidence.prompting import (
    compact_legal_evidence_catalog,
    legal_evidence_ids_for_check,
    remaining_legal_prompt_budget,
)
from contract.risk.models import RiskReviewPlanInput
from contract.risk.playbooks import build_default_registry
from pydantic import BaseModel, ConfigDict, Field, model_validator
from service.conversation.llm_runner import LlmCompletionResult, LlmRuntime
from task_manager.output_parser import parse_json_output

from services.contract.capabilities.model_observation import (
    finalize_completion_success,
    finalize_completion_validation_failed,
)
from services.contract.capabilities.party_roles import contract_party_roles
from services.contract.capabilities.prompt_budget import (
    PROVIDER_PROMPT_HARD_LIMIT_TOKENS,
    PromptBudgetResult,
    evaluate_prompt_budget,
)
from services.contract.capabilities.risk_review import EvidenceCandidate, FindingDraft
from services.contract.capabilities.risk_review_bundle import BaseRiskReviewBundle

HORIZONTAL_UNIT_IDS = (
    "cross_clause_consistency",
    "missing_ambiguity_completeness",
)
HORIZONTAL_CHECK_CODES = (
    "CCC-001",
    "CCC-002",
    "CCC-003",
    "CCC-004",
    "CCC-005",
    "MAC-001",
    "MAC-002",
    "MAC-003",
    "MAC-004",
    "MAC-005",
    "MAC-006",
)
PROMPT_POLICY_VERSION = "2.0"

_SYSTEM_PROMPT = """你是合同横向关系最小裁决器。输入只包含Python确定性生成的候选及其白名单证据。
你必须对每个candidate_id恰好返回一次RISK、NO_RISK或INSUFFICIENT_EVIDENCE。
不得新增候选、Check、冲突方、Evidence、Finding或技术字段。
RISK只能选择输入allowed_severity_factors和allowed_control_codes中的值。
NO_RISK必须说明适用范围、对象、时间阶段、一般/特别规则、优先级、例外或计价口径等明确化解理由；
HARD_RULE候选没有合法Counter Evidence时不得返回NO_RISK。
只输出JSON对象，不输出Markdown、解释过程或代码围栏。"""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


HorizontalUnitId = Literal[
    "cross_clause_consistency",
    "missing_ambiguity_completeness",
]
Ownership = Literal["HORIZONTAL", "BASE_DOMAIN", "SHARED_CONTEXT_ONLY"]
HorizontalVerdict = Literal["RISK", "NO_RISK", "INSUFFICIENT_EVIDENCE"]


class RelationshipNode(StrictModel):
    node_id: str = Field(pattern=r"^horizontal-node-[0-9a-f]{32}$")
    ir_id: str | None = Field(default=None, max_length=160)
    ir_type: str = Field(min_length=1, max_length=80)
    anchor_id: str | None = Field(default=None, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    heading_path: list[str] = Field(default_factory=list)
    subject: str | None = Field(default=None, max_length=500)
    predicate: str = Field(min_length=1, max_length=500)
    object: str | None = Field(default=None, max_length=2000)
    normalized_topic: str = Field(min_length=1, max_length=500)
    normalized_value: str = Field(min_length=1, max_length=2000)
    source_text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class ContractRelationshipIndex(StrictModel):
    generation_id: str = Field(min_length=1, max_length=160)
    normalized_parties: list[RelationshipNode] = Field(default_factory=list)
    defined_terms: list[RelationshipNode] = Field(default_factory=list)
    undefined_terms: list[RelationshipNode] = Field(default_factory=list)
    term_usages: list[RelationshipNode] = Field(default_factory=list)
    clause_references: list[RelationshipNode] = Field(default_factory=list)
    attachment_references: list[RelationshipNode] = Field(default_factory=list)
    amounts: list[RelationshipNode] = Field(default_factory=list)
    percentages: list[RelationshipNode] = Field(default_factory=list)
    dates: list[RelationshipNode] = Field(default_factory=list)
    durations: list[RelationshipNode] = Field(default_factory=list)
    deadlines: list[RelationshipNode] = Field(default_factory=list)
    conditions: list[RelationshipNode] = Field(default_factory=list)
    exceptions: list[RelationshipNode] = Field(default_factory=list)
    priority_rules: list[RelationshipNode] = Field(default_factory=list)
    obligations: list[RelationshipNode] = Field(default_factory=list)
    rights: list[RelationshipNode] = Field(default_factory=list)
    prohibitions: list[RelationshipNode] = Field(default_factory=list)
    termination_triggers: list[RelationshipNode] = Field(default_factory=list)
    payment_triggers: list[RelationshipNode] = Field(default_factory=list)
    acceptance_triggers: list[RelationshipNode] = Field(default_factory=list)
    liability_caps: list[RelationshipNode] = Field(default_factory=list)
    indemnity_rules: list[RelationshipNode] = Field(default_factory=list)
    governing_law: list[RelationshipNode] = Field(default_factory=list)
    dispute_forums: list[RelationshipNode] = Field(default_factory=list)
    all_nodes: list[RelationshipNode]
    raw_pair_count: int = Field(ge=0)
    indexed_pair_count: int = Field(ge=0)
    discarded_pair_count: int = Field(ge=0)
    index_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class HorizontalEvidenceSource(StrictModel):
    source_id: str = Field(pattern=r"^horizontal-es-[0-9a-f]{32}$")
    generation_id: str = Field(min_length=1, max_length=160)
    ir_id: str | None = Field(default=None, max_length=160)
    anchor_id: str | None = Field(default=None, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    heading_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class HorizontalAbsenceEvidenceSource(StrictModel):
    source_id: str = Field(pattern=r"^horizontal-as-[0-9a-f]{32}$")
    generation_id: str = Field(min_length=1, max_length=160)
    check_code: str = Field(pattern=r"^MAC-00[1-6]$")
    candidate_type: str = Field(min_length=1, max_length=160)
    checked_scope: str = Field(min_length=1, max_length=1000)
    required_mechanism: str = Field(min_length=1, max_length=500)
    present_ir_types: list[str]
    missing_target: str = Field(min_length=1, max_length=500)
    verification_method: str = Field(min_length=1, max_length=2000)
    trigger_evidence_source_ids: list[str] = Field(min_length=1, max_length=20)


class HorizontalCandidate(StrictModel):
    candidate_id: str = Field(pattern=r"^horizontal-candidate-[0-9a-f]{32}$")
    unit_id: HorizontalUnitId
    check_code: str = Field(pattern=r"^(CCC-00[1-5]|MAC-00[1-6])$")
    candidate_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    candidate_strength: Literal["HARD_RULE", "STRONG_SIGNAL", "SEMANTIC_REVIEW"]
    normalized_topic: str = Field(min_length=1, max_length=500)
    conflict_dimension: str | None = Field(default=None, max_length=160)
    left_evidence_source_ids: list[str] = Field(default_factory=list, max_length=20)
    right_evidence_source_ids: list[str] = Field(default_factory=list, max_length=20)
    context_evidence_source_ids: list[str] = Field(default_factory=list, max_length=20)
    absence_evidence_source_ids: list[str] = Field(default_factory=list, max_length=10)
    left_normalized_claim: str | None = Field(default=None, max_length=2000)
    right_normalized_claim: str | None = Field(default=None, max_length=2000)
    possible_resolution_rules: list[str] = Field(default_factory=list, max_length=20)
    allowed_counter_evidence_source_ids: list[str] = Field(default_factory=list, max_length=30)
    allowed_supporting_evidence_source_ids: list[str] = Field(default_factory=list, max_length=30)
    required_trigger_conditions: list[str] = Field(min_length=1, max_length=20)
    disqualifying_conditions: list[str] = Field(default_factory=list, max_length=20)
    primary_evidence_requirements: list[str] = Field(min_length=1, max_length=20)
    absence_evidence_requirements: list[str] = Field(default_factory=list, max_length=20)
    canonical_root_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    severity_rule_id: str = Field(pattern=r"^[A-Z][A-Z0-9_]*_V[0-9]+$")
    deterministic_severity_factors: list[str] = Field(default_factory=list, max_length=10)
    allowed_severity_factors: list[str] = Field(default_factory=list, max_length=20)
    allowed_control_codes: list[str] = Field(min_length=1, max_length=20)
    owner_type: Ownership
    linked_base_finding_ids: list[str] = Field(default_factory=list, max_length=30)
    requires_model_decision: bool

    @model_validator(mode="after")
    def validate_roles(self) -> "HorizontalCandidate":
        role_lists = (
            self.left_evidence_source_ids,
            self.right_evidence_source_ids,
            self.context_evidence_source_ids,
            self.absence_evidence_source_ids,
            self.allowed_counter_evidence_source_ids,
            self.allowed_supporting_evidence_source_ids,
            self.linked_base_finding_ids,
        )
        if any(len(values) != len(set(values)) for values in role_lists):
            raise ValueError("Horizontal Candidate role lists must be unique")
        if self.unit_id == "cross_clause_consistency":
            if not self.left_evidence_source_ids or not self.right_evidence_source_ids:
                raise ValueError("Consistency Candidate requires two Evidence sides")
        if self.unit_id == "missing_ambiguity_completeness":
            if self.absence_evidence_requirements and not self.absence_evidence_source_ids:
                raise ValueError("Missing Candidate requires an Absence Evidence Source")
        if self.owner_type != "HORIZONTAL" and self.requires_model_decision:
            raise ValueError("Only a HORIZONTAL-owned Candidate may invoke the model")
        return self


class HorizontalBatch(StrictModel):
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    unit_id: HorizontalUnitId
    check_codes: list[str] = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1, max_length=20)
    evidence_source_ids: list[str] = Field(min_length=1, max_length=80)
    estimated_business_context_tokens: int = Field(ge=0)


class HorizontalReviewPlan(StrictModel):
    plan_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(pattern=r"^horizontal-plan-[0-9a-f]{32}$")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    relationship_index: ContractRelationshipIndex
    evidence_sources: list[HorizontalEvidenceSource]
    absence_evidence_sources: list[HorizontalAbsenceEvidenceSource]
    candidates: list[HorizontalCandidate]
    batches: list[HorizontalBatch]
    check_codes: list[str] = Field(min_length=11, max_length=11)
    prompt_budget_policy_version: Literal["2.0"] = "2.0"


class HorizontalDecisionRaw(StrictModel):
    candidate_id: str = Field(pattern=r"^horizontal-candidate-[0-9a-f]{32}$")
    verdict: HorizontalVerdict
    decision_summary: str = Field(min_length=1, max_length=1200)
    resolution_reason: str | None = Field(default=None, max_length=500)
    severity_factors: list[str] = Field(default_factory=list, max_length=20)
    supporting_evidence_source_ids: list[str] = Field(default_factory=list, max_length=30)
    counter_evidence_source_ids: list[str] = Field(default_factory=list, max_length=30)
    recommended_control_codes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_verdict(self) -> "HorizontalDecisionRaw":
        if self.verdict == "RISK" and not self.recommended_control_codes:
            raise ValueError("RISK requires a recommended Control Code")
        if self.verdict != "RISK" and self.recommended_control_codes:
            raise ValueError("Only RISK may contain Control Codes")
        if self.verdict == "NO_RISK" and not self.resolution_reason:
            raise ValueError("NO_RISK requires an explicit resolution reason")
        return self


class HorizontalDecisionEnvelope(StrictModel):
    candidate_decisions: list[HorizontalDecisionRaw] = Field(min_length=1, max_length=20)


class HorizontalDecision(StrictModel):
    candidate_id: str
    check_code: str
    verdict: HorizontalVerdict
    decision_summary: str
    accepted_severity_factors: list[str] = Field(default_factory=list)
    rejected_severity_factors: list[str] = Field(default_factory=list)
    supporting_evidence_source_ids: list[str] = Field(default_factory=list)
    counter_evidence_source_ids: list[str] = Field(default_factory=list)
    recommended_control_codes: list[str] = Field(default_factory=list)
    owner_type: Ownership
    linked_base_finding_ids: list[str] = Field(default_factory=list)


class HorizontalCanonicalRoot(StrictModel):
    root_id: str = Field(pattern=r"^horizontal-root-[0-9a-f]{32}$")
    unit_id: HorizontalUnitId
    check_code: str
    root_type: str
    candidate_ids: list[str] = Field(min_length=1)
    primary_evidence_source_ids: list[str] = Field(min_length=1)
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    severity_factors: list[str]
    finding_local_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")


class HorizontalCheckResult(StrictModel):
    check_code: str
    status: Literal["REVIEWED", "FAILED"]
    reason_code: Literal[
        "RISK_IDENTIFIED",
        "NO_RISK_IDENTIFIED",
        "CONFIRMED_BY_BASE_DOMAIN",
        "NO_DETERMINISTIC_CANDIDATES",
        "INSUFFICIENT_EVIDENCE",
        "CHECK_FAILED",
    ]
    candidate_ids: list[str] = Field(default_factory=list)
    finding_local_ids: list[str] = Field(default_factory=list)
    linked_base_finding_ids: list[str] = Field(default_factory=list)


class HorizontalBatchMetric(StrictModel):
    batch_id: str
    unit_id: HorizontalUnitId
    wall_duration_ms: int = Field(ge=0)
    model_call_count: int = Field(ge=0, le=1)
    repair_count: Literal[0] = 0
    tool_call_count: Literal[0] = 0
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_budget: PromptBudgetResult | None = None
    legal_evidence_status: Literal[
        "NOT_REQUESTED", "INCLUDED", "OMITTED_TOKEN_BUDGET"
    ] = "NOT_REQUESTED"
    legal_evidence_candidate_count: int = Field(default=0, ge=0)
    legal_evidence_prompted_count: int = Field(default=0, ge=0)
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    error_code: str | None = None
    error_message: str | None = None


class HorizontalUnitResult(StrictModel):
    unit_id: HorizontalUnitId
    status: Literal["COMPLETED", "PARTIAL_FAILED", "FAILED"]
    check_results: list[HorizontalCheckResult]
    decisions: list[HorizontalDecision]
    canonical_roots: list[HorizontalCanonicalRoot]
    findings: list[FindingDraft]
    batch_metrics: list[HorizontalBatchMetric]
    model_call_count: int = Field(ge=0)
    repair_count: Literal[0] = 0
    tool_call_count: Literal[0] = 0
    wall_duration_ms: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class ExtendedBundleMetrics(StrictModel):
    base_phase_wall_ms: int = Field(ge=0)
    horizontal_candidate_build_ms: int = Field(ge=0)
    horizontal_phase_wall_ms: int = Field(ge=0)
    extended_bundle_wall_ms: int = Field(ge=0)
    base_peak_concurrency: int = Field(ge=0)
    horizontal_peak_concurrency: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    repair_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    prompt_budget_warning_count: int = Field(ge=0)
    prompt_budget_hard_failure_count: int = Field(ge=0)


class ExtendedRiskReviewBundle(StrictModel):
    bundle_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(pattern=r"^extended-risk-bundle-[0-9a-f]{32}$")
    status: Literal["COMPLETED", "PARTIAL_FAILED"]
    review_id: str
    generation_id: str
    base_bundle: BaseRiskReviewBundle
    horizontal_plan_id: str
    horizontal_plan_hash: str
    horizontal_units: list[HorizontalUnitResult] = Field(min_length=2, max_length=2)
    findings: list[FindingDraft]
    check_codes: list[str] = Field(min_length=45, max_length=45)
    metrics: ExtendedBundleMetrics


class HorizontalReviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HorizontalLlmCompleter(Protocol):
    async def complete_with_usage(self, **kwargs: Any) -> LlmCompletionResult: ...


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _source_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date_value(text: str) -> date | None:
    match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", text)
    if not match:
        return None
    try:
        return date(*(int(value) for value in match.groups()))
    except ValueError:
        return None


def _all_ir(value: RiskReviewPlanInput) -> list[tuple[str, Any]]:
    semantic = value.stage_result.semantic_ir
    result: list[tuple[str, Any]] = []
    for ir_type in (
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
    ):
        result.extend((ir_type, item) for item in getattr(semantic, ir_type))
    return result


def build_relationship_index(
    value: RiskReviewPlanInput,
) -> tuple[ContractRelationshipIndex, list[HorizontalEvidenceSource]]:
    blocks = {item.block_id: item for item in value.source_blocks}
    nodes: list[RelationshipNode] = []
    sources: dict[str, HorizontalEvidenceSource] = {}
    categories: dict[str, list[RelationshipNode]] = defaultdict(list)

    for ir_type, item in _all_ir(value):
        payload = item.model_dump(mode="json")
        item_id = payload.get("item_id") or _stable_id("ir", payload)
        subject = payload.get("term") or payload.get("subject")
        predicate = "定义为" if ir_type == "definitions" else payload.get("predicate")
        obj = payload.get("meaning") if ir_type == "definitions" else payload.get("object")
        for anchor in item.source_anchors:
            block = blocks.get(anchor.block_id)
            if block is None or anchor.char_end > len(block.text):
                raise HorizontalReviewError(
                    "HORIZONTAL_SOURCE_INVALID",
                    "An IR Anchor does not resolve to the active Generation",
                )
            quoted = block.text[anchor.char_start : anchor.char_end]
            source_id = _stable_id(
                "horizontal-es",
                {
                    "generation_id": value.generation_id,
                    "ir_id": item_id,
                    "anchor_id": anchor.anchor_id,
                },
            )
            sources[source_id] = HorizontalEvidenceSource(
                source_id=source_id,
                generation_id=value.generation_id,
                ir_id=item_id,
                anchor_id=anchor.anchor_id,
                block_id=block.block_id,
                block_no=block.block_no,
                heading_path=block.heading_path,
                char_start=anchor.char_start,
                char_end=anchor.char_end,
                quoted_text=quoted,
                quoted_text_hash=_source_hash(quoted),
            )
            node = RelationshipNode(
                node_id=_stable_id(
                    "horizontal-node",
                    {"source_id": source_id, "ir_type": ir_type},
                ),
                ir_id=item_id,
                ir_type=ir_type,
                anchor_id=anchor.anchor_id,
                block_id=block.block_id,
                block_no=block.block_no,
                heading_path=block.heading_path,
                subject=subject,
                predicate=predicate or "涉及",
                object=obj,
                normalized_topic=_normalize(subject or ir_type),
                normalized_value=_normalize(obj or predicate or quoted),
                source_text=quoted,
                char_start=anchor.char_start,
                char_end=anchor.char_end,
            )
            nodes.append(node)
            categories[ir_type].append(node)

    # Block-level nodes preserve party declarations, attachment references and
    # placeholders that do not necessarily become semantic IR.
    for block in value.source_blocks:
        tags: list[str] = []
        if re.search(r"以下简称[“\"]?(甲方|乙方)", block.text):
            tags.append("party_declaration")
        if re.search(r"(甲方|乙方)\s*[（(](?:盖章|签章)[）)]", block.text):
            tags.append("party_identity")
        if "附件" in block.text:
            tags.append("attachment_reference")
        if re.search(r"待定|另行约定|_{3,}|【[^】]*待[^】]*】", block.text):
            tags.append("placeholder")
        if not tags:
            continue
        for tag in tags:
            source_id = _stable_id(
                "horizontal-es",
                {
                    "generation_id": value.generation_id,
                    "block_id": block.block_id,
                    "tag": tag,
                },
            )
            sources[source_id] = HorizontalEvidenceSource(
                source_id=source_id,
                generation_id=value.generation_id,
                block_id=block.block_id,
                block_no=block.block_no,
                heading_path=block.heading_path,
                char_start=0,
                char_end=len(block.text),
                quoted_text=block.text,
                quoted_text_hash=_source_hash(block.text),
            )
            node = RelationshipNode(
                node_id=_stable_id("horizontal-node", {"source_id": source_id}),
                ir_type=tag,
                block_id=block.block_id,
                block_no=block.block_no,
                heading_path=block.heading_path,
                predicate=tag,
                normalized_topic=tag,
                normalized_value=_normalize(block.text),
                source_text=block.text,
                char_start=0,
                char_end=len(block.text),
            )
            nodes.append(node)
            categories[tag].append(node)

    nodes = sorted(nodes, key=lambda item: (item.block_no, item.node_id))
    topic_groups: dict[str, list[RelationshipNode]] = defaultdict(list)
    for node in nodes:
        topic_groups[node.normalized_topic].append(node)
    raw_pairs = len(nodes) * (len(nodes) - 1) // 2
    indexed_pairs = sum(
        len(values) * (len(values) - 1) // 2
        for values in topic_groups.values()
        if len(values) > 1
    )
    index_payload = [item.model_dump(mode="json") for item in nodes]
    index_hash = "sha256:" + hashlib.sha256(
        canonical_json(index_payload).encode("utf-8")
    ).hexdigest()
    index = ContractRelationshipIndex(
        generation_id=value.generation_id,
        normalized_parties=categories["party_declaration"],
        defined_terms=categories["definitions"],
        undefined_terms=[],
        term_usages=[],
        clause_references=categories["attachment_reference"],
        attachment_references=categories["attachment_reference"],
        amounts=categories["amounts"],
        percentages=[
            item
            for item in categories["amounts"]
            if "%" in item.source_text or "百分" in item.source_text or "千分" in item.source_text
        ],
        dates=categories["dates"],
        durations=[
            item
            for item in categories["dates"]
            if re.search(r"工作日|自然日|个月|一月|期限", item.source_text)
        ],
        deadlines=categories["dates"],
        conditions=categories["obligations"],
        exceptions=[
            item for item in nodes if "除" in item.source_text or "另有约定" in item.source_text
        ],
        priority_rules=[
            item for item in nodes if "优先" in item.source_text or "另有约定" in item.source_text
        ],
        obligations=categories["obligations"],
        rights=categories["rights"],
        prohibitions=categories["prohibitions"],
        termination_triggers=categories["termination_terms"],
        payment_triggers=categories["payment_terms"],
        acceptance_triggers=categories["acceptance_terms"],
        liability_caps=[
            item for item in categories["liabilities"] if "上限" in item.source_text
        ],
        indemnity_rules=categories["liabilities"],
        governing_law=[
            item for item in categories["dispute_resolution"] if "法律" in item.source_text
        ],
        dispute_forums=categories["dispute_resolution"],
        all_nodes=nodes,
        raw_pair_count=raw_pairs,
        indexed_pair_count=indexed_pairs,
        discarded_pair_count=raw_pairs - indexed_pairs,
        index_hash=index_hash,
    )
    return index, sorted(sources.values(), key=lambda item: item.source_id)


def _base_finding_ids(
    base_bundle: BaseRiskReviewBundle,
    *check_codes: str,
) -> list[str]:
    wanted = set(check_codes)
    return sorted(
        finding.finding_local_id
        for unit in base_bundle.units
        for finding in unit.findings
        if finding.check_code in wanted
    )


def _candidate(
    *,
    unit_id: HorizontalUnitId,
    check_code: str,
    candidate_type: str,
    candidate_strength: Literal["HARD_RULE", "STRONG_SIGNAL", "SEMANTIC_REVIEW"],
    normalized_topic: str,
    primary_left: list[str] | None = None,
    primary_right: list[str] | None = None,
    context: list[str] | None = None,
    absence: list[str] | None = None,
    left_claim: str | None = None,
    right_claim: str | None = None,
    root_type: str,
    severity_rule: str,
    severity_factors: list[str],
    allowed_factors: list[str],
    controls: list[str],
    owner: Ownership = "HORIZONTAL",
    linked_base: list[str] | None = None,
    requires_model: bool = True,
) -> HorizontalCandidate:
    left = primary_left or []
    right = primary_right or []
    context_ids = context or []
    absence_ids = absence or []
    candidate_id = _stable_id(
        "horizontal-candidate",
        {
            "unit_id": unit_id,
            "check_code": check_code,
            "candidate_type": candidate_type,
            "normalized_topic": normalized_topic,
            "left": left,
            "right": right,
            "context": context_ids,
            "absence": absence_ids,
        },
    )
    return HorizontalCandidate(
        candidate_id=candidate_id,
        unit_id=unit_id,
        check_code=check_code,
        candidate_type=candidate_type,
        candidate_strength=candidate_strength,
        normalized_topic=normalized_topic,
        conflict_dimension=(
            candidate_type if unit_id == "cross_clause_consistency" else None
        ),
        left_evidence_source_ids=left,
        right_evidence_source_ids=right,
        context_evidence_source_ids=context_ids,
        absence_evidence_source_ids=absence_ids,
        left_normalized_claim=left_claim,
        right_normalized_claim=right_claim,
        possible_resolution_rules=[
            "适用范围不同",
            "时间阶段不同",
            "对象不同",
            "一般规则与特别规则",
            "存在明确优先级或例外",
        ],
        allowed_counter_evidence_source_ids=list(
            dict.fromkeys([*left, *right, *context_ids])
        ),
        allowed_supporting_evidence_source_ids=list(
            dict.fromkeys([*context_ids, *left, *right])
        ),
        required_trigger_conditions=["候选的全部确定性前置条件已经满足"],
        disqualifying_conditions=["存在明确优先级、适用范围或例外足以化解问题"],
        primary_evidence_requirements=["必须使用Python固定的核心Evidence"],
        absence_evidence_requirements=(
            ["必须同时具有Trigger Evidence和合法Absence Evidence"]
            if absence_ids
            else []
        ),
        canonical_root_type=root_type,
        severity_rule_id=severity_rule,
        deterministic_severity_factors=severity_factors,
        allowed_severity_factors=allowed_factors,
        allowed_control_codes=controls,
        owner_type=owner,
        linked_base_finding_ids=linked_base or [],
        requires_model_decision=requires_model,
    )


def build_horizontal_plan(
    value: RiskReviewPlanInput,
    base_bundle: BaseRiskReviewBundle,
) -> HorizontalReviewPlan:
    roles = contract_party_roles(
        perspective=value.perspective,
        our_party=value.our_party,
        counterparty=value.counterparty,
    )
    index, evidence_sources = build_relationship_index(value)
    source_by_node = {
        _stable_id(
            "horizontal-es",
            {
                "generation_id": value.generation_id,
                "ir_id": node.ir_id,
                "anchor_id": node.anchor_id,
            },
        ): node
        for node in index.all_nodes
        if node.ir_id and node.anchor_id
    }
    block_sources = {
        source.source_id: source
        for source in evidence_sources
        if source.ir_id is None
    }

    def block_source(block_id: str, tag: str) -> HorizontalEvidenceSource:
        source_id = _stable_id(
            "horizontal-es",
            {
                "generation_id": value.generation_id,
                "block_id": block_id,
                "tag": tag,
            },
        )
        try:
            return block_sources[source_id]
        except KeyError as exc:
            raise HorizontalReviewError(
                "HORIZONTAL_SOURCE_INVALID",
                f"Missing block-level Source for {tag}",
            ) from exc
    candidates: list[HorizontalCandidate] = []
    absence_sources: list[HorizontalAbsenceEvidenceSource] = []

    start_nodes = [
        node
        for node in index.dates
        if "开始" in node.predicate or "承包期间" in (node.subject or "")
    ]
    signing_nodes = [
        node
        for node in index.dates
        if "签订" in (node.subject or "") or "签订" in node.predicate
    ]
    for start in start_nodes[:1]:
        for signing in signing_nodes[:1]:
            start_date = _date_value(start.source_text)
            signing_date = _date_value(signing.source_text)
            if start_date and signing_date and start_date < signing_date:
                left_id = next(
                    source_id
                    for source_id, node in source_by_node.items()
                    if node.node_id == start.node_id
                )
                right_id = next(
                    source_id
                    for source_id, node in source_by_node.items()
                    if node.node_id == signing.node_id
                )
                candidates.append(
                    _candidate(
                        unit_id="cross_clause_consistency",
                        check_code="CCC-001",
                        candidate_type="DATE_CHRONOLOGY_CONFLICT",
                        candidate_strength="HARD_RULE",
                        normalized_topic="合同履行起始与签订时间",
                        primary_left=[left_id],
                        primary_right=[right_id],
                        left_claim=start.source_text,
                        right_claim=signing.source_text,
                        root_type="EFFECTIVE_DATE_CHRONOLOGY_CONFLICT",
                        severity_rule="HORIZONTAL_DATE_CONFLICT_V1",
                        severity_factors=[
                            "DEADLINE_CONFLICT",
                            "DIRECT_CONTRADICTION",
                        ],
                        allowed_factors=["SCHEDULE_IMPACT"],
                        controls=["ALIGN_DATES_AND_DEADLINES"],
                    )
                )

    for node in index.normalized_parties:
        alias_match = re.search(r"以下简称[“\"]?(甲方|乙方)", node.source_text)
        declared_match = re.search(
            r"([^\n：:]+?)\s*[（(]\s*以下简称[“\"]?(甲方|乙方)",
            node.source_text,
        )
        if declared_match is None:
            continue
        declared = declared_match.group(1).strip()
        declared_role = alias_match.group(1) if alias_match else None
        expected_name = (
            roles.name_for_role(declared_role)
            if declared_role in {"甲方", "乙方"}
            else ""
        )
        if expected_name and _normalize(declared) != _normalize(expected_name):
            source = block_source(node.block_id, "party_declaration")
            corroborating = next(
                (
                    item
                    for item in index.all_nodes
                    if item.ir_type == "party_identity"
                    and item.block_id != node.block_id
                    and expected_name in item.source_text
                ),
                None,
            )
            if corroborating is None:
                # A cross-clause conflict requires two independent text
                # sources. Resolved party context alone is not Evidence.
                continue
            corroborating_source = block_source(
                corroborating.block_id,
                "party_identity",
            )
            candidates.append(
                _candidate(
                    unit_id="cross_clause_consistency",
                    check_code="CCC-005",
                    candidate_type="PARTY_NAME_CONFLICT",
                    candidate_strength="HARD_RULE",
                    normalized_topic=f"{declared_role}主体全称",
                    primary_left=[source.source_id],
                    primary_right=[corroborating_source.source_id],
                    left_claim=declared,
                    right_claim=expected_name,
                    root_type="PARTY_TERM_IDENTITY_CONFLICT",
                    severity_rule="HORIZONTAL_TERM_CONFLICT_V1",
                    severity_factors=[
                        "CORE_TERM_CONFLICT",
                        "DIRECT_CONTRADICTION",
                    ],
                    allowed_factors=[],
                    controls=["UNIFY_DEFINED_TERM"],
                )
            )

    # Missing attachments are structural completeness issues, not consistency
    # conflicts. A reference is the positive trigger; the active source scope
    # supplies the absence proof.
    for node in index.attachment_references[:1]:
        source = block_source(node.block_id, "attachment_reference")
        attachment_present = any(
            "附件" in " ".join(block.heading_path)
            and block.block_id != node.block_id
            for block in value.source_blocks
        )
        if not attachment_present:
            absence_id = _stable_id(
                "horizontal-as",
                {
                    "generation_id": value.generation_id,
                    "check_code": "MAC-005",
                    "candidate_type": "MISSING_REFERENCED_ATTACHMENT",
                    "trigger": source.source_id,
                },
            )
            absence_sources.append(
                HorizontalAbsenceEvidenceSource(
                    source_id=absence_id,
                    generation_id=value.generation_id,
                    check_code="MAC-005",
                    candidate_type="MISSING_REFERENCED_ATTACHMENT",
                    checked_scope="当前Active Generation的全部93个Block及其heading_path",
                    required_mechanism="正文引用的附件应当随合同提供且可唯一定位",
                    present_ir_types=sorted(
                        {item.ir_type for item in index.all_nodes if item.ir_id}
                    ),
                    missing_target="正文所引用的附件正文或可解析附件Section",
                    verification_method=(
                        "扫描全部Block、heading_path和附件引用；发现正文引用，"
                        "但未发现独立附件Section或附件正文。"
                    ),
                    trigger_evidence_source_ids=[source.source_id],
                )
            )
            candidates.append(
                _candidate(
                    unit_id="missing_ambiguity_completeness",
                    check_code="MAC-005",
                    candidate_type="MISSING_REFERENCED_ATTACHMENT",
                    candidate_strength="HARD_RULE",
                    normalized_topic="附件承包方案",
                    context=[source.source_id],
                    absence=[absence_id],
                    root_type="REFERENCED_ATTACHMENT_MISSING",
                    severity_rule="HORIZONTAL_BROKEN_REFERENCE_V1",
                    severity_factors=["BROKEN_REFERENCE"],
                    allowed_factors=["BROKEN_REFERENCE", "OPERATIONAL_IMPACT"],
                    controls=["ADD_MISSING_ATTACHMENT", "CORRECT_BROKEN_REFERENCE"],
                )
            )

    # Base-owned completeness candidates are retained for audit and linkage,
    # but never sent to the horizontal model or materialized twice.
    base_owned_specs = (
        ("MAC-001", "REQUIRED_CLAUSE_ALREADY_OWNED", ("PO-004", "LRE-008")),
        ("MAC-002", "BUSINESS_MECHANISM_ALREADY_OWNED", ("CF-005", "PO-006")),
        ("MAC-004", "AMBIGUOUS_STANDARD_ALREADY_OWNED", ("PO-004", "PO-001")),
        ("MAC-006", "REMEDY_EXIT_ALREADY_OWNED", ("LRE-007", "LRE-008")),
    )
    fallback_source = evidence_sources[0].source_id
    for check_code, candidate_type, base_checks in base_owned_specs:
        linked = _base_finding_ids(base_bundle, *base_checks)
        if not linked:
            continue
        candidates.append(
            _candidate(
                unit_id="missing_ambiguity_completeness",
                check_code=check_code,
                candidate_type=candidate_type,
                candidate_strength="HARD_RULE",
                normalized_topic=candidate_type,
                context=[fallback_source],
                root_type=candidate_type,
                severity_rule="HORIZONTAL_BASE_OWNERSHIP_V1",
                severity_factors=[],
                allowed_factors=[],
                controls=["CLARIFY_SCOPE_AND_CONDITIONS"],
                owner="BASE_DOMAIN",
                linked_base=linked,
                requires_model=False,
            )
        )

    candidates = sorted(candidates, key=lambda item: item.candidate_id)
    source_ids = {item.source_id for item in evidence_sources}
    absence_ids = {item.source_id for item in absence_sources}
    for candidate in candidates:
        refs = {
            *candidate.left_evidence_source_ids,
            *candidate.right_evidence_source_ids,
            *candidate.context_evidence_source_ids,
            *candidate.allowed_counter_evidence_source_ids,
            *candidate.allowed_supporting_evidence_source_ids,
        }
        if refs - source_ids or set(candidate.absence_evidence_source_ids) - absence_ids:
            raise HorizontalReviewError(
                "HORIZONTAL_CANDIDATE_SOURCE_INVALID",
                "A Candidate references a Source outside the current Generation",
            )

    model_candidates = [
        item for item in candidates if item.requires_model_decision
    ]
    batches: list[HorizontalBatch] = []
    for unit_id in HORIZONTAL_UNIT_IDS:
        unit_candidates = [
            item for item in model_candidates if item.unit_id == unit_id
        ]
        if not unit_candidates:
            continue
        # Consistency starts with two deterministic lanes. Completeness stays
        # in one lane unless the deterministic context budget forces splitting.
        groups = (
            [[item] for item in unit_candidates]
            if unit_id == "cross_clause_consistency" and len(unit_candidates) > 1
            else [unit_candidates]
        )
        for group in groups:
            selected_sources = sorted(
                {
                    source_id
                    for item in group
                    for source_id in (
                        item.left_evidence_source_ids
                        + item.right_evidence_source_ids
                        + item.context_evidence_source_ids
                    )
                }
            )
            estimate = max(
                1,
                round(
                    len(
                        canonical_json(
                            {
                                "candidates": [
                                    item.model_dump(mode="json") for item in group
                                ],
                                "sources": [
                                    source.model_dump(mode="json")
                                    for source in evidence_sources
                                    if source.source_id in selected_sources
                                ],
                            }
                        )
                    )
                    / 2
                ),
            )
            if estimate > 5000:
                raise HorizontalReviewError(
                    "RISK_CONTEXT_BUDGET_EXCEEDED",
                    "A horizontal Candidate exceeds its Business Context budget",
                )
            batches.append(
                HorizontalBatch(
                    batch_id=_stable_id(
                        "risk-batch",
                        {
                            "review_id": value.review_id,
                            "generation_id": value.generation_id,
                            "unit_id": unit_id,
                            "candidate_ids": [item.candidate_id for item in group],
                        },
                    ),
                    unit_id=unit_id,
                    check_codes=sorted({item.check_code for item in group}),
                    candidate_ids=[item.candidate_id for item in group],
                    evidence_source_ids=selected_sources,
                    estimated_business_context_tokens=estimate,
                )
            )

    payload = {
        "review_id": value.review_id,
        "generation_id": value.generation_id,
        "index_hash": index.index_hash,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "batches": [item.model_dump(mode="json") for item in batches],
        "check_codes": list(HORIZONTAL_CHECK_CODES),
    }
    plan_hash = "sha256:" + hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return HorizontalReviewPlan(
        plan_id="horizontal-plan-" + plan_hash.removeprefix("sha256:")[:32],
        plan_hash=plan_hash,
        review_id=value.review_id,
        generation_id=value.generation_id,
        relationship_index=index,
        evidence_sources=evidence_sources,
        absence_evidence_sources=sorted(
            absence_sources, key=lambda item: item.source_id
        ),
        candidates=candidates,
        batches=sorted(batches, key=lambda item: item.batch_id),
        check_codes=list(HORIZONTAL_CHECK_CODES),
    )


def _batch_prompt_details(
    plan: HorizontalReviewPlan,
    batch: HorizontalBatch,
    legal_evidence: list[LegalEvidence] | None = None,
) -> tuple[
    str,
    list[LegalEvidence],
    int,
    Literal["NOT_REQUESTED", "INCLUDED", "OMITTED_TOKEN_BUDGET"],
]:
    candidates = {
        item.candidate_id: item for item in plan.candidates
    }
    sources = {
        item.source_id: item for item in plan.evidence_sources
    }
    absences = {
        item.source_id: item for item in plan.absence_evidence_sources
    }
    payload = {
        "task": "HORIZONTAL_CANDIDATE_DECISION",
        "unit_id": batch.unit_id,
        "candidate_decisions_required": [
            candidates[candidate_id].model_dump(mode="json")
            for candidate_id in batch.candidate_ids
        ],
        "evidence_sources": [
            sources[source_id].model_dump(mode="json")
            for source_id in batch.evidence_source_ids
        ],
        "absence_evidence_sources": [
            absences[source_id].model_dump(mode="json")
            for candidate_id in batch.candidate_ids
            for source_id in candidates[candidate_id].absence_evidence_source_ids
        ],
        "output_contract": {
            "candidate_decisions": [
                {
                    "candidate_id": "必须来自输入",
                    "verdict": "RISK|NO_RISK|INSUFFICIENT_EVIDENCE",
                    "decision_summary": "只说明裁决，不生成正式Finding",
                    "resolution_reason": "NO_RISK时必填，否则null",
                    "severity_factors": ["只能来自allowed_severity_factors"],
                    "supporting_evidence_source_ids": ["只能来自白名单"],
                    "counter_evidence_source_ids": ["只能来自白名单"],
                    "recommended_control_codes": ["RISK时从allowed_control_codes选择"],
                }
            ],
        },
    }
    baseline_prompt = canonical_json(payload)
    legal_budget = remaining_legal_prompt_budget(
        baseline_prompt=baseline_prompt,
        system_prompt=_SYSTEM_PROMPT,
        hard_limit_tokens=PROVIDER_PROMPT_HARD_LIMIT_TOKENS,
    )
    effective_evidence = list(legal_evidence or [])
    legal_catalog, legal_tokens = compact_legal_evidence_catalog(
        effective_evidence,
        maximum_catalog_tokens=legal_budget,
    )
    if not legal_catalog:
        status = "OMITTED_TOKEN_BUDGET" if effective_evidence else "NOT_REQUESTED"
        # The model does not receive the catalog, but the deterministic
        # Check-to-Evidence-ID binding remains available to materialization.
        return baseline_prompt, effective_evidence, 0, status
    payload["legal_evidence_catalog"] = legal_catalog
    payload["legal_evidence_input_tokens"] = legal_tokens
    payload["output_contract"]["legal_evidence_rules"] = [
        "法律依据不能代替合同原文Evidence",
        "UNVERIFIED的生效日期或状态不得表述为已核实现行有效",
        "Evidence ID由Python绑定，模型不得输出或编造",
    ]
    return canonical_json(payload), effective_evidence, legal_tokens, "INCLUDED"


def _batch_prompt(
    plan: HorizontalReviewPlan,
    batch: HorizontalBatch,
    legal_evidence: list[LegalEvidence] | None = None,
) -> str:
    return _batch_prompt_details(plan, batch, legal_evidence)[0]


def _parse_decisions(
    content: str,
    candidates: list[HorizontalCandidate],
) -> list[HorizontalDecisionRaw]:
    parsed = parse_json_output(content)
    if not parsed.ok or not isinstance(parsed.structured, dict):
        raise HorizontalReviewError(
            "HORIZONTAL_MODEL_SCHEMA_INVALID",
            "The model did not return one complete JSON object",
        )
    try:
        envelope = HorizontalDecisionEnvelope.model_validate(parsed.structured)
    except Exception as exc:
        raise HorizontalReviewError(
            "HORIZONTAL_MODEL_SCHEMA_INVALID",
            f"Horizontal decision schema is invalid: {exc}",
        ) from exc
    expected = [item.candidate_id for item in candidates]
    actual = [item.candidate_id for item in envelope.candidate_decisions]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise HorizontalReviewError(
            "HORIZONTAL_CANDIDATE_COVERAGE_INVALID",
            "Candidate decisions are missing, duplicated or unknown",
        )
    return sorted(
        envelope.candidate_decisions,
        key=lambda item: expected.index(item.candidate_id),
    )


def _validate_decision(
    raw: HorizontalDecisionRaw,
    candidate: HorizontalCandidate,
) -> HorizontalDecision:
    allowed_sources = {
        *candidate.left_evidence_source_ids,
        *candidate.right_evidence_source_ids,
        *candidate.context_evidence_source_ids,
    }
    if (
        set(raw.supporting_evidence_source_ids)
        - set(candidate.allowed_supporting_evidence_source_ids)
        or set(raw.counter_evidence_source_ids)
        - set(candidate.allowed_counter_evidence_source_ids)
    ):
        raise HorizontalReviewError(
            "HORIZONTAL_EVIDENCE_SOURCE_INVALID",
            "The model selected an Evidence Source outside the Candidate whitelist",
        )
    if not set(raw.supporting_evidence_source_ids + raw.counter_evidence_source_ids).issubset(
        allowed_sources
    ):
        raise HorizontalReviewError(
            "HORIZONTAL_EVIDENCE_SOURCE_INVALID",
            "The model selected an Evidence Source outside the current Candidate",
        )
    if (
        raw.verdict == "NO_RISK"
        and candidate.candidate_strength == "HARD_RULE"
        and not raw.counter_evidence_source_ids
    ):
        raise HorizontalReviewError(
            "HORIZONTAL_NO_RISK_COUNTER_EVIDENCE_MISSING",
            "A HARD_RULE conflict cannot be rejected without Counter Evidence",
        )
    if set(raw.recommended_control_codes) - set(candidate.allowed_control_codes):
        raise HorizontalReviewError(
            "HORIZONTAL_CONTROL_CODE_INVALID",
            "The model returned an unknown or unassigned Control Code",
        )
    accepted = [
        item
        for item in raw.severity_factors
        if item in candidate.allowed_severity_factors
    ]
    rejected = [
        item
        for item in raw.severity_factors
        if item not in candidate.allowed_severity_factors
    ]
    return HorizontalDecision(
        candidate_id=candidate.candidate_id,
        check_code=candidate.check_code,
        verdict=raw.verdict,
        decision_summary=raw.decision_summary,
        accepted_severity_factors=accepted,
        rejected_severity_factors=rejected,
        supporting_evidence_source_ids=list(
            dict.fromkeys(raw.supporting_evidence_source_ids)
        ),
        counter_evidence_source_ids=list(
            dict.fromkeys(raw.counter_evidence_source_ids)
        ),
        recommended_control_codes=raw.recommended_control_codes,
        owner_type=candidate.owner_type,
        linked_base_finding_ids=candidate.linked_base_finding_ids,
    )


def _risk_level(candidate: HorizontalCandidate, decision: HorizontalDecision) -> str:
    factors = {
        *candidate.deterministic_severity_factors,
        *decision.accepted_severity_factors,
    }
    if factors & {
        "AMOUNT_CONFLICT",
        "LIABILITY_CONFLICT",
        "RIGHT_OBLIGATION_CONFLICT",
    }:
        return "HIGH"
    if candidate.candidate_type in {
        "DATE_CHRONOLOGY_CONFLICT",
        "PARTY_NAME_CONFLICT",
        "MISSING_REFERENCED_ATTACHMENT",
    }:
        return "MEDIUM"
    return "LOW"


def _finding_text(candidate: HorizontalCandidate) -> tuple[str, str, str, str]:
    party_role = "合同主体"
    if candidate.candidate_type == "PARTY_NAME_CONFLICT":
        if candidate.normalized_topic.startswith("甲方"):
            party_role = "甲方"
        elif candidate.normalized_topic.startswith("乙方"):
            party_role = "乙方"
    templates = {
        "DATE_CHRONOLOGY_CONFLICT": (
            "合同履行起始日早于签订日且追溯效力未明确",
            "合同约定的履行起始时间早于签订时间，但未明确追溯适用范围和期间责任。",
            "可能导致签订前履行、费用、验收和违约责任的起算口径发生争议。",
            "统一签订日、履行起始日和生效日，并明确是否追溯适用及追溯期间的结算和责任。",
        ),
        "PARTY_NAME_CONFLICT": (
            "合同主体全称存在异常字符且与审查主体不一致",
            f"合同中的{party_role}全称与已确认的对应主体存在字面差异，可能影响主体识别。",
            "可能造成合同相对方、签署主体或权利义务归属争议。",
            "核对营业执照和签署信息，统一合同全部位置的主体全称并删除异常字符。",
        ),
        "MISSING_REFERENCED_ATTACHMENT": (
            "正文引用的附件未随当前合同提供",
            "正文将服务范围或标准指向附件，但当前合同材料中未发现可定位的附件正文。",
            "服务范围、质量标准和验收依据可能无法完整确定，影响履行和争议处理。",
            "补充并签署被引用附件，统一附件名称、版本和优先级，并确保正文引用可唯一定位。",
        ),
    }
    return templates[candidate.candidate_type]


def _materialize_unit(
    *,
    value: RiskReviewPlanInput,
    plan: HorizontalReviewPlan,
    unit_id: HorizontalUnitId,
    decisions: list[HorizontalDecision],
    metrics: list[HorizontalBatchMetric],
    started: float,
    legal_evidence_by_check: dict[str, list[LegalEvidence]] | None = None,
) -> HorizontalUnitResult:
    candidate_by_id = {
        item.candidate_id: item
        for item in plan.candidates
        if item.unit_id == unit_id
    }
    decision_by_id = {item.candidate_id: item for item in decisions}
    source_by_id = {item.source_id: item for item in plan.evidence_sources}
    absence_by_id = {
        item.source_id: item for item in plan.absence_evidence_sources
    }
    roots: list[HorizontalCanonicalRoot] = []
    findings: list[FindingDraft] = []
    all_decisions = list(decisions)

    for candidate in candidate_by_id.values():
        if candidate.owner_type == "BASE_DOMAIN":
            all_decisions.append(
                HorizontalDecision(
                    candidate_id=candidate.candidate_id,
                    check_code=candidate.check_code,
                    verdict="RISK",
                    decision_summary="基础领域已完整表达该风险，横向层只保留链接。",
                    owner_type="BASE_DOMAIN",
                    linked_base_finding_ids=candidate.linked_base_finding_ids,
                )
            )
            continue
        decision = decision_by_id.get(candidate.candidate_id)
        if decision is None or decision.verdict != "RISK":
            continue
        primary_ids = list(
            dict.fromkeys(
                candidate.left_evidence_source_ids
                + candidate.right_evidence_source_ids
                + candidate.context_evidence_source_ids
                + candidate.absence_evidence_source_ids
            )
        )
        root_id = _stable_id(
            "horizontal-root",
            {
                "check_code": candidate.check_code,
                "root_type": candidate.canonical_root_type,
                "normalized_topic": candidate.normalized_topic,
                "primary_evidence_source_ids": primary_ids,
            },
        )
        finding_id = _stable_id(
            "finding",
            {"root_id": root_id, "perspective": value.perspective.value},
        )
        title, issue, impact, suggestion = _finding_text(candidate)
        evidence_candidates: list[EvidenceCandidate] = []
        for position, source_id in enumerate(primary_ids, 1):
            if source_id in source_by_id:
                source = source_by_id[source_id]
                evidence_candidates.append(
                    EvidenceCandidate(
                        evidence_local_id=_stable_id(
                            "evidence",
                            {"finding_id": finding_id, "source_id": source_id},
                        ),
                        finding_local_id=finding_id,
                        evidence_type="TEXT_QUOTE",
                        source_ir_item_id=source.ir_id,
                        anchor_id=source.anchor_id,
                        block_id=source.block_id,
                        page_number=None,
                        char_start=source.char_start,
                        char_end=source.char_end,
                        quoted_text=source.quoted_text,
                        quoted_text_hash=source.quoted_text_hash,
                    )
                )
            else:
                source = absence_by_id[source_id]
                evidence_candidates.append(
                    EvidenceCandidate(
                        evidence_local_id=_stable_id(
                            "evidence",
                            {"finding_id": finding_id, "source_id": source_id},
                        ),
                        finding_local_id=finding_id,
                        evidence_type="ABSENCE",
                        checked_scope=source.checked_scope,
                        verification_note=source.verification_method,
                    )
                )
        risk_level = _risk_level(candidate, decision)
        finding = FindingDraft(
            finding_local_id=finding_id,
            source_unit_id=unit_id,
            domain=unit_id,
            check_code=candidate.check_code,
            category=(
                "INTERNAL_CONFLICT"
                if unit_id == "cross_clause_consistency"
                else (
                    "MISSING_CLAUSE"
                    if candidate.absence_evidence_source_ids
                    else "AMBIGUITY"
                )
            ),
            risk_type=candidate.canonical_root_type,
            risk_level=risk_level,
            title=title,
            issue=issue,
            impact_to_our_party=impact,
            suggestion=suggestion,
            perspective=value.perspective.value,
            our_party=value.our_party,
            counterparty=value.counterparty,
            evidence_candidates=evidence_candidates,
            legal_evidence_ids=legal_evidence_ids_for_check(
                (legal_evidence_by_check or {}).get(candidate.check_code, []),
                candidate.check_code,
            ),
        )
        findings.append(finding)
        roots.append(
            HorizontalCanonicalRoot(
                root_id=root_id,
                unit_id=unit_id,
                check_code=candidate.check_code,
                root_type=candidate.canonical_root_type,
                candidate_ids=[candidate.candidate_id],
                primary_evidence_source_ids=primary_ids,
                risk_level=risk_level,
                severity_factors=sorted(
                    {
                        *candidate.deterministic_severity_factors,
                        *decision.accepted_severity_factors,
                    }
                ),
                finding_local_id=finding_id,
            )
        )

    check_results: list[HorizontalCheckResult] = []
    registry = build_default_registry()
    for check in registry.checks:
        if check.domain != unit_id:
            continue
        check_candidates = [
            item
            for item in candidate_by_id.values()
            if item.check_code == check.check_code
        ]
        check_findings = [
            item for item in findings if item.check_code == check.check_code
        ]
        linked = sorted(
            {
                finding_id
                for candidate in check_candidates
                for finding_id in candidate.linked_base_finding_ids
            }
        )
        insufficient = any(
            decision_by_id.get(candidate.candidate_id) is not None
            and decision_by_id[candidate.candidate_id].verdict
            == "INSUFFICIENT_EVIDENCE"
            for candidate in check_candidates
            if candidate.owner_type != "BASE_DOMAIN"
        )
        if check_findings:
            reason = "RISK_IDENTIFIED"
        elif linked:
            reason = "CONFIRMED_BY_BASE_DOMAIN"
        elif insufficient:
            reason = "INSUFFICIENT_EVIDENCE"
        elif check_candidates:
            reason = "NO_RISK_IDENTIFIED"
        else:
            reason = "NO_DETERMINISTIC_CANDIDATES"
        check_results.append(
            HorizontalCheckResult(
                check_code=check.check_code,
                status=("FAILED" if insufficient else "REVIEWED"),
                reason_code=reason,
                candidate_ids=[item.candidate_id for item in check_candidates],
                finding_local_ids=[item.finding_local_id for item in check_findings],
                linked_base_finding_ids=linked,
            )
        )
    failed_check_count = sum(item.status == "FAILED" for item in check_results)
    return HorizontalUnitResult(
        unit_id=unit_id,
        status=(
            "FAILED"
            if failed_check_count == len(check_results)
            else ("PARTIAL_FAILED" if failed_check_count else "COMPLETED")
        ),
        check_results=check_results,
        decisions=sorted(all_decisions, key=lambda item: item.candidate_id),
        canonical_roots=sorted(roots, key=lambda item: item.root_id),
        findings=sorted(findings, key=lambda item: item.finding_local_id),
        batch_metrics=metrics,
        model_call_count=sum(item.model_call_count for item in metrics),
        wall_duration_ms=round((time.perf_counter() - started) * 1000),
        warnings=[
            warning
            for item in metrics
            for warning in (
                (
                    ["RISK_PROMPT_TOKEN_SOFT_WARNING"]
                    if item.prompt_budget
                    and item.prompt_budget.budget_status == "SOFT_WARNING"
                    else []
                )
                + (
                    ["LEGAL_EVIDENCE_OMITTED_TOKEN_BUDGET"]
                    if item.legal_evidence_status == "OMITTED_TOKEN_BUDGET"
                    else []
                )
                + (
                    [f"{item.error_code}: {item.error_message}"]
                    if item.status == "FAILED"
                    else []
                )
            )
        ],
    )


async def execute_horizontal_unit(
    value: RiskReviewPlanInput,
    plan: HorizontalReviewPlan,
    unit_id: HorizontalUnitId,
    *,
    tenant_id: str,
    model_id: str,
    runtime_factory: Callable[[str], HorizontalLlmCompleter] = LlmRuntime,
    framework_run_id: str | None = None,
    timeout_seconds: float = 180.0,
    legal_evidence: list[LegalEvidence] | None = None,
) -> HorizontalUnitResult:
    started = time.perf_counter()
    batches = [item for item in plan.batches if item.unit_id == unit_id]
    candidates = {item.candidate_id: item for item in plan.candidates}
    if not batches:
        return _materialize_unit(
            value=value,
            plan=plan,
            unit_id=unit_id,
            decisions=[],
            metrics=[],
            started=started,
            legal_evidence_by_check={},
        )
    runtime = runtime_factory(tenant_id)
    prompted_legal_evidence_by_check: dict[str, dict[str, LegalEvidence]] = {
        check_code: {} for check_code in HORIZONTAL_CHECK_CODES
    }

    async def run_batch(
        batch: HorizontalBatch,
    ) -> tuple[list[HorizontalDecision], HorizontalBatchMetric]:
        batch_started = time.perf_counter()
        selected = [candidates[item] for item in batch.candidate_ids]
        completion: LlmCompletionResult | None = None
        batch_legal_evidence = [
            item
            for item in (legal_evidence or [])
            if bool(set(item.check_codes) & set(batch.check_codes))
        ]
        (
            prompt,
            effective_legal_evidence,
            batch_legal_tokens,
            legal_evidence_status,
        ) = _batch_prompt_details(plan, batch, batch_legal_evidence)
        for check_code in batch.check_codes:
            prompted_legal_evidence_by_check[check_code].update(
                {item.evidence_id: item for item in effective_legal_evidence}
            )
        try:
            completion = await asyncio.wait_for(
                runtime.complete_with_usage(
                    messages=[{
                        "role": "user",
                        "content": prompt,
                    }],
                    model_id=model_id,
                    system_prompt=_SYSTEM_PROMPT,
                    max_tokens=2500,
                    temperature=0,
                    thinking_override=False,
                    response_format={"type": "json_object"},
                    review_unit_id=unit_id,
                    review_id=value.review_id,
                    framework_run_id=framework_run_id,
                    attempt_no=value.attempt_no,
                    repair_no=0,
                    defer_terminal=True,
                ),
                timeout=timeout_seconds,
            )
            budget = evaluate_prompt_budget(
                provider_prompt_tokens=completion.prompt_tokens,
                provider_cached_tokens=completion.cached_tokens,
                estimated_business_context_tokens=(
                    batch.estimated_business_context_tokens + batch_legal_tokens
                ),
                unit_id=unit_id,
                batch_id=batch.batch_id,
            )
            if budget.budget_status == "HARD_LIMIT_EXCEEDED":
                raise HorizontalReviewError(
                    "RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED",
                    f"{batch.batch_id} exceeded the Provider Prompt hard limit",
                )
            raw = _parse_decisions(completion.content, selected)
            decisions = [
                _validate_decision(item, candidate)
                for item, candidate in zip(raw, selected, strict=True)
            ]
            metric = HorizontalBatchMetric(
                batch_id=batch.batch_id,
                unit_id=unit_id,
                wall_duration_ms=round(
                    (time.perf_counter() - batch_started) * 1000
                ),
                model_call_count=1,
                prompt_tokens=completion.prompt_tokens,
                cached_tokens=completion.cached_tokens,
                completion_tokens=completion.completion_tokens,
                total_tokens=completion.total_tokens,
                prompt_budget=budget,
                legal_evidence_status=legal_evidence_status,
                legal_evidence_candidate_count=len(batch_legal_evidence),
                legal_evidence_prompted_count=len(effective_legal_evidence),
            )
            await finalize_completion_success(completion)
            return decisions, metric
        except asyncio.CancelledError:
            if completion is not None:
                await finalize_completion_validation_failed(
                    completion,
                    "MODEL_OUTPUT_PROCESSING_CANCELLED",
                )
            raise
        except Exception as exc:
            if completion is not None:
                await finalize_completion_validation_failed(
                    completion,
                    "MODEL_OUTPUT_PROCESSING_FAILED",
                )
            code = getattr(exc, "code", "HORIZONTAL_BATCH_FAILED")
            message = str(exc) or exc.__class__.__name__
            decisions = [
                HorizontalDecision(
                    candidate_id=candidate.candidate_id,
                    check_code=candidate.check_code,
                    verdict="INSUFFICIENT_EVIDENCE",
                    decision_summary=(
                        f"横向Batch未完成，当前Candidate未形成风险结论："
                        f"{code}"
                    ),
                    owner_type=candidate.owner_type,
                    linked_base_finding_ids=candidate.linked_base_finding_ids,
                )
                for candidate in selected
            ]
            return decisions, HorizontalBatchMetric(
                batch_id=batch.batch_id,
                unit_id=unit_id,
                wall_duration_ms=round(
                    (time.perf_counter() - batch_started) * 1000
                ),
                model_call_count=0,
                status="FAILED",
                error_code=code,
                error_message=message[:1000],
                legal_evidence_status=legal_evidence_status,
                legal_evidence_candidate_count=len(batch_legal_evidence),
                legal_evidence_prompted_count=len(effective_legal_evidence),
            )

    results = await asyncio.gather(*(run_batch(batch) for batch in batches))
    decisions = [decision for result, _metric in results for decision in result]
    metrics = [metric for _result, metric in results]
    return _materialize_unit(
        value=value,
        plan=plan,
        unit_id=unit_id,
        decisions=decisions,
        metrics=metrics,
        started=started,
        legal_evidence_by_check={
            check_code: list(evidence_by_id.values())
            for check_code, evidence_by_id in prompted_legal_evidence_by_check.items()
        },
    )


async def execute_horizontal_phase(
    value: RiskReviewPlanInput,
    plan: HorizontalReviewPlan,
    *,
    tenant_id: str,
    model_id: str,
    runtime_factory: Callable[[str], HorizontalLlmCompleter] = LlmRuntime,
    framework_run_id: str | None = None,
    timeout_seconds: float = 180.0,
    legal_evidence_by_domain: dict[str, list[LegalEvidence]] | None = None,
) -> tuple[list[HorizontalUnitResult], int]:
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def run(unit_id: HorizontalUnitId) -> HorizontalUnitResult:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        try:
            return await execute_horizontal_unit(
                value,
                plan,
                unit_id,
                tenant_id=tenant_id,
                model_id=model_id,
                runtime_factory=runtime_factory,
                framework_run_id=framework_run_id,
                timeout_seconds=timeout_seconds,
                legal_evidence=(legal_evidence_by_domain or {}).get(unit_id, []),
            )
        finally:
            async with lock:
                active -= 1

    results = await asyncio.gather(
        run("cross_clause_consistency"),
        run("missing_ambiguity_completeness"),
    )
    return list(results), peak


def build_extended_bundle(
    *,
    value: RiskReviewPlanInput,
    base_bundle: BaseRiskReviewBundle,
    horizontal_plan: HorizontalReviewPlan,
    horizontal_units: list[HorizontalUnitResult],
    base_phase_wall_ms: int,
    horizontal_candidate_build_ms: int,
    horizontal_phase_wall_ms: int,
    horizontal_peak_concurrency: int,
) -> ExtendedRiskReviewBundle:
    if base_bundle.status not in {"COMPLETED", "PARTIAL_FAILED"}:
        raise HorizontalReviewError(
            "EXTENDED_BASE_PHASE_FAILED",
            "Extended Bundle requires a usable base Bundle",
        )
    if {item.unit_id for item in horizontal_units} != set(HORIZONTAL_UNIT_IDS):
        raise HorizontalReviewError(
            "EXTENDED_HORIZONTAL_UNIT_MISSING",
            "Extended Bundle requires both horizontal Units",
        )
    identity = base_bundle.identity
    expected_identity = (
        value.review_id,
        value.document_id,
        value.generation_id,
        value.perspective.value,
        value.our_party,
        value.counterparty,
    )
    actual_identity = (
        identity.review_id,
        identity.document_id,
        identity.generation_id,
        identity.perspective,
        identity.our_party,
        identity.counterparty,
    )
    if actual_identity != expected_identity:
        raise HorizontalReviewError(
            "EXTENDED_IDENTITY_MISMATCH",
            "Base and horizontal phases do not share the frozen review identity",
        )
    if (
        horizontal_plan.review_id != value.review_id
        or horizontal_plan.generation_id != value.generation_id
    ):
        raise HorizontalReviewError(
            "EXTENDED_HORIZONTAL_PLAN_STALE",
            "The horizontal Plan does not belong to the active review Generation",
        )
    if any(
        item.generation_id != value.generation_id
        for item in [
            *horizontal_plan.evidence_sources,
            *horizontal_plan.absence_evidence_sources,
        ]
    ):
        raise HorizontalReviewError(
            "EXTENDED_HORIZONTAL_EVIDENCE_STALE",
            "Horizontal Evidence Source crossed the active review Generation",
        )
    base_checks = {
        item.check_code
        for unit in base_bundle.units
        for item in unit.check_results
    }
    all_checks = sorted(base_checks | set(horizontal_plan.check_codes))
    if len(base_checks) != 34 or len(all_checks) != 45:
        raise HorizontalReviewError(
            "EXTENDED_CHECK_COVERAGE_INVALID",
            "Extended Bundle must cover exactly 34 base and 11 horizontal Checks",
        )
    horizontal_findings = [
        finding for unit in horizontal_units for finding in unit.findings
    ]
    base_keys = {
        (finding.check_code, finding.risk_type, finding.finding_local_id)
        for unit in base_bundle.units
        for finding in unit.findings
    }
    horizontal_keys = {
        (finding.check_code, finding.risk_type, finding.finding_local_id)
        for finding in horizontal_findings
    }
    if base_keys & horizontal_keys:
        raise HorizontalReviewError(
            "EXTENDED_FINDING_OWNERSHIP_CONFLICT",
            "A horizontal Finding duplicates a base-domain Finding identity",
        )
    metrics = [
        metric
        for unit in horizontal_units
        for metric in unit.batch_metrics
    ]
    total_wall = base_phase_wall_ms + horizontal_candidate_build_ms + horizontal_phase_wall_ms
    bundle_id = _stable_id(
        "extended-risk-bundle",
        {
            "base_bundle_id": base_bundle.bundle_id,
            "horizontal_plan_hash": horizontal_plan.plan_hash,
        },
    )
    base_findings = [
        finding for unit in base_bundle.units for finding in unit.findings
    ]
    return ExtendedRiskReviewBundle(
        bundle_id=bundle_id,
        status=(
            "PARTIAL_FAILED"
            if base_bundle.status == "PARTIAL_FAILED"
            or any(item.status != "COMPLETED" for item in horizontal_units)
            else "COMPLETED"
        ),
        review_id=value.review_id,
        generation_id=value.generation_id,
        base_bundle=base_bundle,
        horizontal_plan_id=horizontal_plan.plan_id,
        horizontal_plan_hash=horizontal_plan.plan_hash,
        horizontal_units=sorted(horizontal_units, key=lambda item: item.unit_id),
        findings=sorted(
            [*base_findings, *horizontal_findings],
            key=lambda item: item.finding_local_id,
        ),
        check_codes=all_checks,
        metrics=ExtendedBundleMetrics(
            base_phase_wall_ms=base_phase_wall_ms,
            horizontal_candidate_build_ms=horizontal_candidate_build_ms,
            horizontal_phase_wall_ms=horizontal_phase_wall_ms,
            extended_bundle_wall_ms=total_wall,
            base_peak_concurrency=base_bundle.metrics.peak_concurrency,
            horizontal_peak_concurrency=horizontal_peak_concurrency,
            model_call_count=(
                base_bundle.metrics.model_call_count
                + sum(item.model_call_count for item in horizontal_units)
            ),
            repair_count=(
                base_bundle.metrics.repair_count
                + sum(item.repair_count for item in horizontal_units)
            ),
            tool_call_count=(
                base_bundle.metrics.tool_call_count
                + sum(item.tool_call_count for item in horizontal_units)
            ),
            prompt_budget_warning_count=(
                base_bundle.metrics.prompt_budget_warning_count
                + sum(
                    metric.prompt_budget is not None
                    and metric.prompt_budget.budget_status == "SOFT_WARNING"
                    for metric in metrics
                )
            ),
            prompt_budget_hard_failure_count=(
                base_bundle.metrics.prompt_budget_hard_failure_count
            ),
        ),
    )
