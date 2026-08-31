from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SRC = ROOT / "services" / "contract" / "src"
CONTRACT_TESTS = ROOT / "services" / "contract" / "tests"
for path in (CONTRACT_SRC, CONTRACT_TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from common.tokenization import estimate_tokens_in_text
from contract.risk.icd_source_policy import (
    ICD_ABSENCE_POLICIES,
    icd_item_matches_check,
)
from contract.risk.lre_source_policy import lre_has_broad_breach_trigger
from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.risk.po_source_policy import po_item_matches_check
from risk_fixture_loader import load_fixed_risk_plan_input
from service.conversation.llm_runner import LlmCompletionResult

from services.contract.capabilities.risk_review import (
    CheckCoverageResult,
    DirectReviewError,
    EvidenceCandidate,
    FindingDraft,
    LlmAttemptDiagnostic,
    LlmCallMetric,
    ReviewUnitResult,
)
from services.contract.capabilities.risk_review_bundle import (
    _GENERIC_SYSTEM_PROMPT,
    _ICD_ALLOWED_CONTROL_CODES,
    _ICD_SEVERITY_FACTOR_POLICIES,
    _LRE_ALLOWED_CONTROL_CODES,
    _LRE_SEVERITY_FACTOR_POLICIES,
    _PO_ALLOWED_CONTROL_CODES,
    _PO_CANDIDATE_SYSTEM_PROMPT,
    _PO_SEVERITY_FACTOR_POLICIES,
    BASE_UNIT_IDS,
    EXPECTED_BASE_CHECK_CODES,
    BaseBundleExecutionError,
    CandidateDecisionResponseRaw,
    CandidateSeverityFactors,
    CanonicalRiskRoot,
    DeterministicRiskCandidate,
    GenericAttemptArtifact,
    GenericBaseDirectReviewer,
    GenericCheckSpec,
    GenericModelFindingDraft,
    GenericReviewRequest,
    ReviewBatchResult,
    _build_generic_candidates,
    _candidate_allowed_control_codes,
    _candidate_allowed_source_ids,
    _candidate_risk_level,
    _canonical_risk_key,
    _generic_prompt,
    _generic_repair_snapshot,
    _icd_factor_has_required_evidence,
    _icd_scene_relevant,
    _join_template_fragments,
    _lre_factor_has_required_evidence,
    _materialize_po_candidate_decisions,
    _merge_equivalent_same_root_findings,
    _merge_lre_cross_batch_roots,
    _parse_po_candidate_output,
    _po_candidate_prompt,
    _po_candidates_share_canonical_root,
    _po_canonical_root_groups,
    _po_evidence_catalog,
    _po_factor_has_required_evidence,
    _po_risk_level,
    _po_severity_factors,
    _resolve_finding_fields,
    _resolve_po_evidence_source_ids,
    _validate_domain_safety,
    _validate_generic_semantic_preservation,
    _validate_icd_domain_safety,
    _validate_po_control_codes,
    _validate_po_semantic_severity_factors,
    bundle_duration_summary,
    execute_base_risk_review_bundle,
    generic_request_from_context,
)
from services.contract.scripts import contract_risk_stage63_bundle as stage63_runner

FIXTURE_ENV = "CONTRACT_RISK_FIXTURE_DIR"
FAILED_PO_ATTEMPT_ENV = "CONTRACT_RISK_FAILED_PO_ATTEMPT_ARTIFACT"
CANDIDATE_PO_ATTEMPT_ENV = "CONTRACT_RISK_CANDIDATE_PO_ATTEMPT_ARTIFACT"
FVA_CODES = tuple(f"FVA-{index:03d}" for index in range(1, 6))
FVA_CATEGORIES = {
    "FVA-001": "PARTY_IDENTIFICATION",
    "FVA-002": "PARTY_IDENTIFICATION",
    "FVA-003": "MISSING_CLAUSE",
    "FVA-004": "AMBIGUITY",
    "FVA-005": "OTHER",
}
FVA_RISK_TYPES = {
    "FVA-001": "PARTY_IDENTITY_RISK",
    "FVA-002": "AUTHORITY_OR_CAPACITY_RISK",
    "FVA-003": "EXECUTION_FORM_MISSING",
    "FVA-004": "EFFECTIVENESS_CONDITION_AMBIGUITY",
    "FVA-005": "MANDATORY_RULE_OR_VALIDITY_RISK",
}


def test_po007_source_policy_excludes_termination_cure_language() -> None:
    item = SimpleNamespace(
        ir_type="obligations",
        item_id="ir-termination-cure",
        subject="乙方",
        predicate="在收到通知后30日内未改正",
        object="甲方可以解除合同",
    )
    excerpt = SimpleNamespace(
        quoted_text=(
            "乙方在收到通知后30日内未改正的，甲方可以解除合同。"
        )
    )
    check = SimpleNamespace(
        check_code="PO-007",
        required_ir_types=["obligations", "dates", "acceptance_terms"],
    )

    assert not po_item_matches_check(item, [excerpt], check)


def test_template_fragments_do_not_duplicate_terminal_punctuation() -> None:
    assert _join_template_fragments(["证据已完整。", "第二段；", "第三段"]) == (
        "证据已完整；第二段；第三段"
    )


def _request() -> GenericReviewRequest:
    quote = "甲方教育科技有限公司与乙方人工智能科技有限公司签订本协议。"
    return GenericReviewRequest(
        review_id="review-base-1",
        document_id="document-base-1",
        generation_id="generation-base-1",
        attempt_no=1,
        plan_id="risk-plan-" + "1" * 32,
        context_hash="sha256:" + "2" * 64,
        unit_id="formation_validity_authority",
        batch_id="risk-batch-" + "3" * 32,
        perspective="PARTY_A",
        our_party="甲方教育科技有限公司",
        counterparty="乙方人工智能科技有限公司",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        assigned_check_specs=[
            {
                "check_code": code,
                "review_question": f"检查{code}",
                "allowed_categories": [FVA_CATEGORIES[code]],
                "allowed_risk_types": [FVA_RISK_TYPES[code]],
                "required_ir_types": ["definitions"],
                "criticality": "REQUIRED",
            }
            for code in FVA_CODES
        ],
        definitions=[
            {
                "ir_type": "definitions",
                "item_id": "ir-party-1",
                "subject": "合同主体",
                "predicate": "包括",
                "object": "甲方和乙方",
                "source_anchors": [{"anchor_id": "anchor-party-1"}],
            }
        ],
        projected_ir_items=[],
        source_excerpts=[
            {
                "anchor_id": "anchor-party-1",
                "block_id": "block-party-1",
                "block_no": 1,
                "page_number": None,
                "char_start": 0,
                "char_end": len(quote),
                "quoted_text": quote,
                "quoted_text_hash": (
                    "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
                ),
                "heading_path": ["合同主体"],
            }
        ],
        present_ir_types=["definitions"],
        missing_ir_types=[],
        estimated_input_tokens=1200,
    )


def test_generic_legal_catalog_budget_omission_is_explicit(monkeypatch) -> None:
    request = _request()
    request.legal_evidence = [object()]  # compactor is isolated below
    monkeypatch.setattr(
        "services.contract.capabilities.risk_review_bundle.compact_legal_evidence_catalog",
        lambda *_args, **_kwargs: ([], 0),
    )

    _generic_prompt(request)

    assert request.legal_evidence == []
    assert request.legal_evidence_prompt_status == "OMITTED_TOKEN_BUDGET"


def test_empty_legal_evidence_keeps_generic_prompt_on_legacy_shape() -> None:
    prompt, _ir_refs, _anchor_refs = _generic_prompt(_request())
    payload = json.loads(prompt.split("\n", 1)[1])
    assert "legal_evidence_catalog" not in payload
    assert "legal_evidence_input_tokens" not in payload
    assert not any(
        "legal_evidence" in rule
        for rule in payload["output_contract"]["rules"]
    )


def _po_request(
    *,
    shared_po001_anchor: bool = False,
    extra_po001_support: bool = False,
    delivery_object: str = "项目交付",
    po003_mode: str = "OUR_PARTY_DUTY",
    perspective: str = "PARTY_B",
) -> GenericReviewRequest:
    our_role = "甲方" if perspective == "PARTY_A" else "乙方"
    counterparty_role = "乙方" if perspective == "PARTY_A" else "甲方"
    scope_object = "甲方在履行中提出的其他要求"
    if shared_po001_anchor:
        scope_object += "并完成项目交付"
    cooperation_row = (
        "cooperation",
        "obligations",
        our_role,
        "应配合提供资料",
        f"{our_role}延迟仍不顺延{counterparty_role}工期",
    )
    if po003_mode == "COUNTERPARTY_NO_CONSEQUENCE":
        cooperation_row = (
            "cooperation",
            "obligations",
            counterparty_role,
            "应配合提供资料",
            "资料应真实有效",
        )
    elif po003_mode == "VALID_COUNTERPARTY_DEPENDENCY":
        cooperation_row = (
            "cooperation",
            "obligations",
            counterparty_role,
            f"应配合提供资料，{our_role}履行依赖该资料，若未提供将导致",
            f"{our_role}延期且{our_role}仍承担违约责任",
        )
    rows = [
        ("scope", "obligations", "乙方", "应予执行", scope_object),
        ("delivery", "delivery_terms", "乙方", "应完成", delivery_object),
        ("control", "rights", "甲方", "有权随时检查并要求", "乙方整改"),
        ("quality", "obligations", "乙方", "应尽量满足", "项目目的"),
        (
            "acceptance",
            "acceptance_terms",
            "甲方",
            "未提出异议视为验收合格",
            "交付物",
        ),
        cooperation_row,
        ("assignment", "prohibitions", "乙方", "不得转委托", "第三方"),
        ("change", "rights", "甲方", "有权单方调整", "范围和工期"),
        ("support", "obligations", "乙方", "应在3小时响应并整改", "服务问题"),
    ]
    if extra_po001_support:
        rows.append(("neutral", "obligations", "乙方", "提供", "服务说明"))
    ir_items = []
    excerpts = []
    for index, (suffix, ir_type, subject, predicate, object_) in enumerate(rows, 1):
        anchor_id = f"anchor-po-{suffix}"
        quote = f"{subject}{predicate}{object_}。"
        ir_items.append(
            {
                "ir_type": ir_type,
                "item_id": f"ir-po-{suffix}",
                "subject": subject,
                "predicate": predicate,
                "object": object_,
                "source_anchors": [{"anchor_id": anchor_id}],
            }
        )
        excerpts.append(
            {
                "anchor_id": anchor_id,
                "block_id": f"block-po-{suffix}",
                "block_no": index,
                "page_number": None,
                "char_start": 0,
                "char_end": len(quote),
                "quoted_text": quote,
                "quoted_text_hash": (
                    "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
                ),
                "heading_path": ["履行条款"],
            }
        )
    required_types = {
        "PO-001": ["rights", "obligations", "delivery_terms"],
        "PO-002": ["rights", "obligations", "prohibitions"],
        "PO-003": ["obligations"],
        "PO-004": ["obligations", "dates", "delivery_terms", "acceptance_terms"],
        "PO-005": ["rights", "obligations", "prohibitions"],
        "PO-006": ["rights", "obligations", "payment_terms"],
        "PO-007": ["obligations", "dates", "acceptance_terms"],
    }
    generation_id = "generation-po-1"
    excerpt_by_anchor = {item["anchor_id"]: item for item in excerpts}
    evidence_sources = []
    for item in ir_items:
        item_source_excerpts = [
            SimpleNamespace(**excerpt_by_anchor[anchor["anchor_id"]])
            for anchor in item["source_anchors"]
        ]
        allowed_check_codes = [
            code
            for code, types in required_types.items()
            if po_item_matches_check(
                SimpleNamespace(**item),
                item_source_excerpts,
                SimpleNamespace(check_code=code, required_ir_types=types),
            )
        ]
        if not allowed_check_codes:
            continue
        for anchor in item["source_anchors"]:
            excerpt = excerpt_by_anchor[anchor["anchor_id"]]
            source_id = RiskReviewPlanBuilder._stable_id(
                "risk-es",
                {
                    "generation_id": generation_id,
                    "ir_item_id": item["item_id"],
                    "anchor_id": excerpt["anchor_id"],
                    "char_start": excerpt["char_start"],
                    "char_end": excerpt["char_end"],
                    "evidence_type": "TEXT_QUOTE",
                },
            )
            evidence_sources.append(
                {
                    "source_id": source_id,
                    "generation_id": generation_id,
                    "ir_item_id": item["item_id"],
                    "anchor_id": excerpt["anchor_id"],
                    "block_id": excerpt["block_id"],
                    "page_number": excerpt["page_number"],
                    "char_start": excerpt["char_start"],
                    "char_end": excerpt["char_end"],
                    "quoted_text": excerpt["quoted_text"],
                    "quoted_text_hash": excerpt["quoted_text_hash"],
                    "evidence_type": "TEXT_QUOTE",
                    "ir_type": item["ir_type"],
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    "object": item["object"],
                    "heading_path": excerpt["heading_path"],
                    "allowed_check_codes": allowed_check_codes,
                }
            )
    present_ir_types = sorted({row[1] for row in rows})
    absence_sources = []
    policies = []
    for code, types in required_types.items():
        checked_scope = f"当前Batch投影的合同IR与Source Excerpt；检查项{code}"
        verification_method = (
            "Python按CheckSpec.required_ir_types及确定性候选扫描当前Batch；"
            "仅证明本次合同文本投影中未定位到目标条款，不推断外部事实。"
        )
        source_id = RiskReviewPlanBuilder._stable_id(
            "risk-as",
            {
                "generation_id": generation_id,
                "check_code": code,
                "checked_scope": checked_scope,
                "verification_method": verification_method,
                "present_ir_types": present_ir_types,
                "missing_target": f"检查{code}",
            },
        )
        absence_sources.append(
            {
                "source_id": source_id,
                "generation_id": generation_id,
                "check_code": code,
                "checked_scope": checked_scope,
                "verification_method": verification_method,
                "present_ir_types": present_ir_types,
                "missing_target": f"检查{code}",
            }
        )
        policies.append(
            {
                "check_code": code,
                "allowed_evidence_source_ids": [
                    item["source_id"]
                    for item in evidence_sources
                    if code in item["allowed_check_codes"]
                ],
                "allowed_absence_source_ids": [source_id],
            }
        )
    return GenericReviewRequest(
        review_id="review-po-1",
        document_id="document-po-1",
        generation_id=generation_id,
        attempt_no=1,
        plan_id="risk-plan-" + "4" * 32,
        context_hash="sha256:" + "5" * 64,
        unit_id="performance_obligations",
        batch_id="risk-batch-" + "6" * 32,
        perspective=perspective,
        our_party=(
            "甲方教育科技有限公司"
            if perspective == "PARTY_A"
            else "乙方人工智能科技有限公司"
        ),
        counterparty=(
            "乙方人工智能科技有限公司"
            if perspective == "PARTY_A"
            else "甲方教育科技有限公司"
        ),
        contract_type="SERVICE",
        review_attitude="NEUTRAL",
        assigned_check_specs=[
            {
                "check_code": code,
                "review_question": f"检查{code}",
                "allowed_categories": ["RIGHTS_OBLIGATIONS_IMBALANCE"],
                "allowed_risk_types": [f"{code}_RISK"],
                "required_ir_types": types,
                "criticality": "REQUIRED",
            }
            for code, types in required_types.items()
        ],
        definitions=[],
        projected_ir_items=ir_items,
        source_excerpts=excerpts,
        evidence_sources=evidence_sources,
        absence_evidence_sources=absence_sources,
        check_evidence_policies=policies,
        present_ir_types=present_ir_types,
        missing_ir_types=["dates", "payment_terms"],
        estimated_input_tokens=3000,
    )


def _icd_request() -> GenericReviewRequest:
    rows = [
        (
            "foreground",
            "intellectual_property_terms",
            "项目成果",
            "知识产权归属于",
            "甲方，乙方仅为履行合同使用",
        ),
        (
            "background",
            "intellectual_property_terms",
            "乙方原有软件和模板",
            "继续归乙方所有并许可甲方",
            "仅为本项目目的使用",
        ),
        (
            "third-party",
            "liabilities",
            "乙方",
            "保证交付成果不侵犯",
            "任何第三方知识产权",
        ),
        (
            "confidentiality",
            "confidentiality_terms",
            "乙方及其人员",
            "应保守",
            "甲方商业秘密",
        ),
        (
            "data-security",
            "obligations",
            "乙方",
            "仅为履行合同处理业务数据并采取访问控制",
            "发生安全事件后及时通知甲方",
        ),
        (
            "data-return",
            "obligations",
            "乙方",
            "应在合同终止后返还并删除",
            "甲方业务数据及备份",
        ),
    ]
    required_types = {
        "ICD-001": ["intellectual_property_terms", "rights", "obligations"],
        "ICD-002": ["intellectual_property_terms", "rights"],
        "ICD-003": ["intellectual_property_terms", "liabilities"],
        "ICD-004": ["confidentiality_terms", "dates", "obligations"],
        "ICD-005": ["confidentiality_terms", "obligations"],
        "ICD-006": ["confidentiality_terms", "rights", "obligations"],
    }
    categories = {
        "ICD-001": "INTELLECTUAL_PROPERTY",
        "ICD-002": "INTELLECTUAL_PROPERTY",
        "ICD-003": "INTELLECTUAL_PROPERTY",
        "ICD-004": "CONFIDENTIALITY",
        "ICD-005": "CONFIDENTIALITY",
        "ICD-006": "CONFIDENTIALITY",
    }
    risk_types = {
        "ICD-001": "FOREGROUND_IP_OWNERSHIP_RISK",
        "ICD-002": "BACKGROUND_IP_LICENSE_RISK",
        "ICD-003": "THIRD_PARTY_IP_RISK",
        "ICD-004": "CONFIDENTIALITY_SCOPE_RISK",
        "ICD-005": "DATA_PROCESSING_RISK",
        "ICD-006": "DATA_RETURN_RETENTION_RISK",
    }
    ir_items = []
    excerpts = []
    for index, (suffix, ir_type, subject, predicate, object_) in enumerate(rows, 1):
        anchor_id = f"anchor-icd-{suffix}"
        quote = f"{subject}{predicate}{object_}。"
        ir_items.append(
            {
                "ir_type": ir_type,
                "item_id": f"ir-icd-{suffix}",
                "subject": subject,
                "predicate": predicate,
                "object": object_,
                "source_anchors": [{"anchor_id": anchor_id}],
            }
        )
        excerpts.append(
            {
                "anchor_id": anchor_id,
                "block_id": f"block-icd-{suffix}",
                "block_no": index,
                "page_number": None,
                "char_start": 0,
                "char_end": len(quote),
                "quoted_text": quote,
                "quoted_text_hash": (
                    "sha256:"
                    + hashlib.sha256(quote.encode("utf-8")).hexdigest()
                ),
                "heading_path": ["知识产权、保密与数据"],
            }
        )
    generation_id = "generation-icd-1"
    excerpt_by_anchor = {item["anchor_id"]: item for item in excerpts}
    evidence_sources = []
    for item in ir_items:
        item_excerpts = [
            SimpleNamespace(**excerpt_by_anchor[anchor["anchor_id"]])
            for anchor in item["source_anchors"]
        ]
        allowed_check_codes = [
            code
            for code, types in required_types.items()
            if icd_item_matches_check(
                SimpleNamespace(**item),
                item_excerpts,
                SimpleNamespace(check_code=code, required_ir_types=types),
            )
        ]
        if not allowed_check_codes:
            continue
        for anchor in item["source_anchors"]:
            excerpt = excerpt_by_anchor[anchor["anchor_id"]]
            source_id = RiskReviewPlanBuilder._stable_id(
                "risk-es",
                {
                    "generation_id": generation_id,
                    "ir_item_id": item["item_id"],
                    "anchor_id": excerpt["anchor_id"],
                    "char_start": excerpt["char_start"],
                    "char_end": excerpt["char_end"],
                    "evidence_type": "TEXT_QUOTE",
                },
            )
            evidence_sources.append(
                {
                    "source_id": source_id,
                    "generation_id": generation_id,
                    "ir_item_id": item["item_id"],
                    "anchor_id": excerpt["anchor_id"],
                    "block_id": excerpt["block_id"],
                    "page_number": None,
                    "char_start": 0,
                    "char_end": len(excerpt["quoted_text"]),
                    "quoted_text": excerpt["quoted_text"],
                    "quoted_text_hash": excerpt["quoted_text_hash"],
                    "evidence_type": "TEXT_QUOTE",
                    "ir_type": item["ir_type"],
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    "object": item["object"],
                    "heading_path": excerpt["heading_path"],
                    "allowed_check_codes": allowed_check_codes,
                }
            )
    present_ir_types = sorted({row[1] for row in rows})
    absence_sources = []
    policies = []
    for code in required_types:
        checked_target, verification_method, missing_target = (
            ICD_ABSENCE_POLICIES[code]
        )
        checked_scope = (
            f"当前Batch全部ICD领域IR与Source Excerpt；"
            f"检查项{code}；检查范围：{checked_target}"
        )
        verification_method = (
            verification_method
            + "；仅证明当前合同技术文本中未定位到该机制，不推断外部事实。"
        )
        source_id = RiskReviewPlanBuilder._stable_id(
            "risk-as",
            {
                "generation_id": generation_id,
                "check_code": code,
                "checked_scope": checked_scope,
                "verification_method": verification_method,
                "present_ir_types": present_ir_types,
                "missing_target": missing_target,
            },
        )
        absence_sources.append(
            {
                "source_id": source_id,
                "generation_id": generation_id,
                "check_code": code,
                "checked_scope": checked_scope,
                "verification_method": verification_method,
                "present_ir_types": present_ir_types,
                "missing_target": missing_target,
            }
        )
        policies.append(
            {
                "check_code": code,
                "allowed_evidence_source_ids": [
                    item["source_id"]
                    for item in evidence_sources
                    if code in item["allowed_check_codes"]
                ],
                "allowed_absence_source_ids": [source_id],
            }
        )
    return GenericReviewRequest(
        review_id="review-icd-1",
        document_id="document-icd-1",
        generation_id=generation_id,
        attempt_no=1,
        plan_id="risk-plan-" + "7" * 32,
        context_hash="sha256:" + "8" * 64,
        unit_id="ip_confidentiality_data",
        batch_id="risk-batch-" + "9" * 32,
        perspective="PARTY_A",
        our_party="甲方教育科技有限公司",
        counterparty="乙方人工智能科技有限公司",
        contract_type="SERVICE",
        review_attitude="NEUTRAL",
        assigned_check_specs=[
            {
                "check_code": code,
                "review_question": f"检查{code}",
                "allowed_categories": [categories[code]],
                "allowed_risk_types": [risk_types[code]],
                "required_ir_types": types,
                "criticality": "REQUIRED",
            }
            for code, types in required_types.items()
        ],
        definitions=[],
        projected_ir_items=ir_items,
        source_excerpts=excerpts,
        evidence_sources=evidence_sources,
        absence_evidence_sources=absence_sources,
        check_evidence_policies=policies,
        present_ir_types=present_ir_types,
        missing_ir_types=["rights", "dates"],
        estimated_input_tokens=2500,
    )


def _po_catalog(request: GenericReviewRequest):
    _text, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    return (
        _po_evidence_catalog(
            request,
            candidates,
            ir_refs,
            anchor_refs,
        ),
        ir_refs,
        anchor_refs,
    )


def _icd_catalog(request: GenericReviewRequest):
    _text, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    return (
        candidates,
        _po_evidence_catalog(
            request,
            candidates,
            ir_refs,
            anchor_refs,
        ),
        ir_refs,
        anchor_refs,
    )


def _candidate(
    suffix: str,
    *,
    check_code: str = "PO-004",
    candidate_type: str = "QUALITY_STANDARD_UNMEASURABLE",
) -> DeterministicRiskCandidate:
    values = dict(
        candidate_id="risk-candidate-" + suffix * 32,
        check_code=check_code,
        candidate_type=candidate_type,
        trigger_reason="固定测试候选",
        required_ir_types=["obligations"],
        candidate_ir_refs=["I001"],
        candidate_evidence_refs=["A001"],
        candidate_strength="HARD_RULE",
        facts=["固定测试事实"],
        trigger_conditions=["固定触发条件"],
        mitigating_conditions=["固定缓释条件"],
        primary_evidence_source_ids=["risk-es-" + "1" * 32],
        allowed_supporting_evidence_source_ids=["risk-es-" + "2" * 32],
        allowed_counter_evidence_source_ids=["risk-es-" + "3" * 32],
        severity_rule_id="PO_QUALITY_STANDARD_V1",
        canonical_root_type="QUALITY_STANDARD_UNMEASURABLE",
        root_severity_rule_id="PO_QUALITY_STANDARD_V1",
        core_primary_evidence_source_ids=["risk-es-" + "1" * 32],
        deterministic_severity_factors=["MISSING_CORE_MECHANISM"],
    )
    if check_code == "PO-003":
        values["requires_model_decision"] = False
        values["po003_precondition"] = {
            "counterparty_cooperation_required": False,
            "performance_depends_on_cooperation": False,
            "adverse_consequence_to_our_party": False,
            "relief_or_adjustment_missing": False,
            "model_review_required": False,
            "supporting_source_ids": [],
            "unmet_conditions": ["测试候选不满足PO-003前置条件"],
        }
    return DeterministicRiskCandidate(**values)


def _payload(
    *,
    fva002_external_assertion: bool = False,
    fva002_assessment: str | None = None,
    fva002_external_required: bool | None = None,
) -> dict:
    results = []
    for code in FVA_CODES:
        findings = []
        if code == "FVA-001":
            findings = [
                {
                    "check_code": code,
                    "category": "PARTY_IDENTIFICATION",
                    "risk_type": "PARTY_IDENTITY_RISK",
                    "risk_level": "MEDIUM",
                    "title": "主体称谓需要统一",
                    "issue": "合同不同位置的主体称谓需要核对并统一。",
                    "impact_to_our_party": "主体指向不明可能影响权利义务归属。",
                    "suggestion": "统一主体全称并核对签署主体。",
                    "evidence": [
                        {
                            "evidence_type": "TEXT_QUOTE",
                            "ir_ref": "I001",
                            "evidence_ref": "A001",
                            "checked_scope": None,
                            "verification_note": None,
                        }
                    ],
                }
            ]
        if code == "FVA-002" and fva002_external_assertion:
            findings = [
                {
                    "check_code": code,
                    "category": "PARTY_IDENTIFICATION",
                    "risk_type": "AUTHORITY_OR_CAPACITY_RISK",
                    "risk_level": "HIGH",
                    "title": "签署人确定无权代理",
                    "issue": "签署人确定未授权。",
                    "impact_to_our_party": "合同必然无效。",
                    "suggestion": "删除合同。",
                    "evidence": [
                        {
                            "evidence_type": "ABSENCE",
                            "ir_ref": None,
                            "evidence_ref": None,
                            "checked_scope": "授权文件",
                            "verification_note": "合同未附授权文件。",
                        }
                    ],
                }
            ]
        result = {
            "check_code": code,
            "status": "REVIEWED",
            "decision_note": "已完成当前检查。",
            "findings": findings,
        }
        if code == "FVA-002":
            assessment = fva002_assessment or (
                "TEXTUAL_AUTHORITY_RISK"
                if fva002_external_assertion
                else "NO_VISIBLE_ISSUE"
            )
            external_required = (
                fva002_external_required
                if fva002_external_required is not None
                else assessment == "EXTERNAL_VERIFICATION_REQUIRED"
            )
            result["assessment_type"] = assessment
            result["external_verification_required"] = external_required
            if assessment == "EXTERNAL_VERIFICATION_REQUIRED":
                result["decision_note"] = (
                    "合同文本未见明确授权冲突；需外部核验法定代表人证明、"
                    "授权委托书或营业执照。"
                )
        results.append(result)
    return {"check_results": results}


def _po_payload(
    *,
    check_code: str = "PO-003",
    evidence_source_ids: list[str],
    include_technical_fields: bool = True,
    candidate_ids: list[str] | None = None,
) -> dict:
    results = []
    for index in range(1, 8):
        code = f"PO-{index:03d}"
        findings = []
        if code == check_code:
            finding = {
                "risk_level": "MEDIUM",
                "title": "配合义务边界需要完善",
                "issue": "合同中的配合义务和延迟归责边界不够完整。",
                "impact_to_our_party": "可能导致我方承担不可控的履约延迟。",
                "suggestion": "明确资料、接口、完成时限和相应顺延后果。",
                "evidence_source_ids": evidence_source_ids,
            }
            if include_technical_fields:
                finding.update(
                    {
                        "check_code": code,
                        "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
                        "risk_type": f"{code}_RISK",
                    }
                )
            if candidate_ids is not None:
                finding["candidate_ids"] = candidate_ids
            findings = [finding]
        results.append(
            {
                "check_code": code,
                "status": "REVIEWED",
                "decision_note": "已完成当前检查。",
                "findings": findings,
            }
        )
    return {"check_results": results}


def _po_candidate_payload(
    request: GenericReviewRequest,
    *,
    risk_check_code: str | None = None,
    risk_candidate_type: str | None = None,
    risk_candidate_types: set[str] | None = None,
) -> dict:
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    decisions = []
    for candidate in candidates:
        if not candidate.requires_model_decision:
            continue
        selected_risk = (
            candidate.check_code == risk_check_code
            and (
                risk_candidate_type is None
                or candidate.candidate_type == risk_candidate_type
            )
            and (
                risk_candidate_types is None
                or candidate.candidate_type in risk_candidate_types
            )
        )
        forced_risk = (
            candidate.candidate_strength in {"HARD_RULE", "STRONG_SIGNAL"}
            and not candidate.allowed_counter_evidence_source_ids
        )
        if selected_risk or forced_risk:
            severity_factors = list(candidate.allowed_severity_factors)
            decision = {
                "candidate_id": candidate.candidate_id,
                "verdict": "RISK",
                "decision_summary": "候选事实满足触发条件，且未识别到有效缓释。",
                "severity_factors": severity_factors,
                "supporting_evidence_source_ids": [],
                "counter_evidence_source_ids": [],
                "recommended_control_codes": [
                    _PO_ALLOWED_CONTROL_CODES[candidate.candidate_type][0]
                ],
            }
        else:
            counters = (
                candidate.allowed_counter_evidence_source_ids[:1]
                if candidate.candidate_strength in {"HARD_RULE", "STRONG_SIGNAL"}
                else []
            )
            decision = {
                "candidate_id": candidate.candidate_id,
                "verdict": "NO_RISK",
                "decision_summary": "反向证据表明该候选在当前合同中未形成实质风险。",
                "severity_factors": [],
                "supporting_evidence_source_ids": [],
                "counter_evidence_source_ids": counters,
                "recommended_control_codes": [],
            }
        decisions.append(decision)
    return {"candidate_decisions": decisions}


def _use_minimal_po001_root_factors(
    request: GenericReviewRequest,
    payload: dict,
) -> None:
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    candidate_types = {
        item.candidate_id: item.candidate_type for item in candidates
    }
    for decision in payload["candidate_decisions"]:
        candidate_type = candidate_types[decision["candidate_id"]]
        if candidate_type == "SCOPE_EXPANSION" and decision["verdict"] == "RISK":
            decision["severity_factors"] = []
        elif (
            candidate_type == "DELIVERY_SCHEDULE_REVIEW"
            and decision["verdict"] == "RISK"
        ):
            decision["severity_factors"] = ["SCHEDULE_IMPACT"]


def _completion(content: str, unit_id: str, repair_no: int = 0) -> LlmCompletionResult:
    return LlmCompletionResult(
        content=content,
        prompt_tokens=1200,
        cached_tokens=0,
        completion_tokens=500,
        total_tokens=1700,
        time_to_first_token_ms=100,
        model_duration_ms=1000,
        trace_id=f"trace-{unit_id}-{repair_no}",
        provider_request_id=f"request-{unit_id}-{repair_no}",
        finish_reason="stop",
        review_unit_id=unit_id,
        review_id="review-base-1",
        framework_run_id="run-base-1",
        attempt_no=1,
        repair_no=repair_no,
    )


@pytest.mark.parametrize(
    "check_code",
    tuple(f"ICD-{index:03d}" for index in range(1, 7)),
)
def test_icd_candidate_registry_covers_all_six_checks(check_code) -> None:
    candidates, _catalog, _ir_refs, _anchor_refs = _icd_catalog(_icd_request())
    assert any(item.check_code == check_code for item in candidates)


def test_icd_candidate_ids_and_order_are_stable() -> None:
    first = _icd_catalog(_icd_request())[0]
    second = _icd_catalog(_icd_request())[0]
    assert [
        (item.candidate_id, item.check_code, item.candidate_type)
        for item in first
    ] == [
        (item.candidate_id, item.check_code, item.candidate_type)
        for item in second
    ]


def test_icd_candidates_never_receive_cross_check_sources() -> None:
    candidates, catalog, _ir_refs, _anchor_refs = _icd_catalog(_icd_request())
    for candidate in candidates:
        allowed = set(
            catalog.allowed_source_ids_by_check[candidate.check_code]
        )
        assert set(candidate.primary_evidence_source_ids) <= allowed
        assert set(candidate.allowed_supporting_evidence_source_ids) <= allowed
        assert set(candidate.allowed_counter_evidence_source_ids) <= allowed


def test_icd_counterparty_confidentiality_duty_is_deterministic_no_risk() -> None:
    candidates, _catalog, _ir_refs, _anchor_refs = _icd_catalog(_icd_request())
    candidate = next(
        item
        for item in candidates
        if item.candidate_type == "CONFIDENTIALITY_PROTECTION_REVIEW"
    )
    assert not candidate.requires_model_decision
    assert candidate.icd_perspective_precondition is not None
    assert candidate.icd_perspective_precondition.counterparty_protects_our_party
    assert not candidate.icd_perspective_precondition.adverse_burden_on_our_party
    assert not candidate.icd_perspective_precondition.model_review_required


def test_icd_our_confidentiality_duty_still_requires_model_review() -> None:
    request = _icd_request().model_copy(
        update={
            "perspective": "PARTY_B",
            "our_party": "乙方人工智能科技有限公司",
            "counterparty": "甲方教育科技有限公司",
        }
    )
    candidates, _catalog, _ir_refs, _anchor_refs = _icd_catalog(request)
    candidate = next(
        item
        for item in candidates
        if item.candidate_type == "CONFIDENTIALITY_PROTECTION_REVIEW"
    )
    assert candidate.requires_model_decision
    assert candidate.icd_perspective_precondition is not None
    assert candidate.icd_perspective_precondition.adverse_burden_on_our_party
    assert candidate.icd_perspective_precondition.model_review_required


@pytest.mark.parametrize(
    "candidate_type",
    tuple(_ICD_ALLOWED_CONTROL_CODES),
)
def test_icd_every_candidate_type_has_frozen_controls(candidate_type) -> None:
    assert _ICD_ALLOWED_CONTROL_CODES[candidate_type]
    assert len(_ICD_ALLOWED_CONTROL_CODES[candidate_type]) == len(
        set(_ICD_ALLOWED_CONTROL_CODES[candidate_type])
    )


@pytest.mark.parametrize(
    "factor_code",
    tuple(_ICD_SEVERITY_FACTOR_POLICIES),
)
def test_icd_severity_factor_registry_is_closed_and_evidence_gated(
    factor_code,
) -> None:
    policy = _ICD_SEVERITY_FACTOR_POLICIES[factor_code]
    assert policy.allowed_check_codes
    assert policy.allowed_candidate_types
    assert policy.required_evidence_types
    assert set(policy.allowed_check_codes) <= {
        f"ICD-{index:03d}" for index in range(1, 7)
    }


@pytest.mark.parametrize(
    ("check_code", "text", "expected"),
    [
        ("ICD-001", "仅交付纸质说明书", False),
        ("ICD-001", "项目开发形成软件成果并交付", True),
        ("ICD-002", "乙方提供普通资料", False),
        ("ICD-002", "乙方原有软件和模板用于本项目", True),
        ("ICD-003", "提供日常咨询服务", False),
        ("ICD-003", "开发并交付软件和源代码", True),
        ("ICD-005", "提供一般培训服务", False),
        ("ICD-005", "乙方处理并存储甲方业务数据", True),
        ("ICD-006", "双方交换普通纸质通知", False),
        ("ICD-006", "乙方访问并存储客户数据", True),
    ],
)
def test_icd_scene_precondition_blocks_unrelated_candidates(
    check_code,
    text,
    expected,
) -> None:
    item = SimpleNamespace(
        subject="合同",
        predicate="约定",
        object=text,
    )
    assert _icd_scene_relevant(check_code, {"I001": item}) is expected


@pytest.mark.parametrize(
    ("factor_code", "text"),
    [
        ("OWNERSHIP_AMBIGUITY", "项目成果知识产权归属不明确"),
        ("OVERBROAD_TRANSFER", "全部知识产权无偿转让给相对方"),
        ("EXCLUSIVE_OR_IRREVOCABLE", "授予永久且不可撤销的排他许可"),
        ("UNLIMITED_SCOPE", "允许在全球任何用途无限制使用"),
        ("POST_TERMINATION_EFFECT", "合同终止后该许可仍永久有效"),
        ("ONE_SIDED_PROTECTION", "仅我方承担保密义务"),
        ("THIRD_PARTY_EXPOSURE", "第三方提出知识产权侵权索赔"),
        ("NO_RETURN_OR_DELETION", "终止后未约定返还或删除数据"),
        ("NO_SECURITY_STANDARD", "合同未约定访问控制和安全措施"),
        ("MISSING_INCIDENT_NOTICE", "缺少数据泄露事件通知时限"),
    ],
)
def test_icd_semantic_factor_requires_literal_or_absence_evidence(
    factor_code,
    text,
) -> None:
    source = _icd_request().evidence_sources[0].model_copy(
        update={
            "subject": "合同",
            "predicate": "约定",
            "object": text,
            "quoted_text": text,
        }
    )
    valid, _reason = _icd_factor_has_required_evidence(
        factor_code,
        text_sources=[source],
        absence_sources=[],
    )
    assert valid


def test_icd_overbroad_transfer_is_not_inferred_from_project_use() -> None:
    source = _icd_request().evidence_sources[0]
    valid, reason = _icd_factor_has_required_evidence(
        "OVERBROAD_TRANSFER",
        text_sources=[source],
        absence_sources=[],
    )
    assert not valid
    assert reason == "REQUIRED_TEXT_SIGNAL_MISSING"


def test_icd_prompt_uses_candidate_decision_only() -> None:
    request = _icd_request()
    candidates, catalog, _ir_refs, _anchor_refs = _icd_catalog(request)
    prompt = _po_candidate_prompt(request, candidates, catalog)
    payload = json.loads(prompt.split("\n", 1)[1])
    fields = payload["output_contract"]["candidate_decision_fields"]
    assert fields == [
        "candidate_id",
        "verdict",
        "decision_summary",
        "severity_factors",
        "supporting_evidence_source_ids",
        "counter_evidence_source_ids",
        "recommended_control_codes",
    ]
    assert {
        "check_code",
        "category",
        "risk_type",
        "risk_level",
        "our_party",
        "counterparty",
        "primary_evidence_source_ids",
    }.isdisjoint(fields)


def test_icd_absence_risk_materializes_one_python_finding() -> None:
    request = _icd_request()
    candidates, catalog, ir_refs, anchor_refs = _icd_catalog(request)
    absence = next(
        item
        for item in candidates
        if item.candidate_type == "CONFIDENTIALITY_COMPLETENESS_ABSENT"
    )
    decisions = []
    for candidate in candidates:
        if not candidate.requires_model_decision:
            continue
        if candidate.candidate_id == absence.candidate_id:
            decisions.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "verdict": "RISK",
                    "decision_summary": "保密条款缺少例外、披露程序和期限机制",
                    "severity_factors": ["MISSING_CORE_MECHANISM"],
                    "supporting_evidence_source_ids": [],
                    "counter_evidence_source_ids": [],
                    "recommended_control_codes": [
                        "ADD_CONFIDENTIALITY_EXCEPTIONS",
                    ],
                }
            )
        else:
            decisions.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "verdict": "NO_RISK",
                    "decision_summary": "现有条款未显示对合同立场不利的实质风险",
                    "severity_factors": [],
                    "supporting_evidence_source_ids": [],
                    "counter_evidence_source_ids": [],
                    "recommended_control_codes": [],
                }
            )
    (
        coverage,
        findings,
        candidate_decisions,
        roots,
        _check_decisions,
        _overlap,
        _perspective_warnings,
        _perspective_conflicts,
    ) = _materialize_po_candidate_decisions(
        request,
        CandidateDecisionResponseRaw(candidate_decisions=decisions),
        candidates,
        catalog,
        ir_refs,
        anchor_refs,
    )
    assert len(coverage) == 6
    assert len(candidate_decisions) == len(candidates)
    assert len(roots) == len(findings) == 1
    assert roots[0].domain == "ip_confidentiality_data"
    assert roots[0].risk_level == "MEDIUM"
    assert findings[0].check_code == "ICD-004"
    assert findings[0].perspective == "PARTY_A"
    assert findings[0].our_party == request.our_party
    assert findings[0].evidence_candidates[0].evidence_type == "ABSENCE"


def test_icd_unvalidated_factor_cannot_raise_final_level() -> None:
    request = _icd_request()
    candidates, catalog, _ir_refs, _anchor_refs = _icd_catalog(request)
    candidate = next(
        item
        for item in candidates
        if item.candidate_type == "FOREGROUND_IP_OWNERSHIP_REVIEW"
    )
    accepted, rejected = _validate_po_semantic_severity_factors(
        candidate,
        ["OVERBROAD_TRANSFER"],
        supporting_ids=[],
        catalog=catalog,
    )
    assert accepted == []
    assert [item.factor_code for item in rejected] == [
        "OVERBROAD_TRANSFER"
    ]
    assert _candidate_risk_level(
        candidate,
        _po_severity_factors(accepted),
    ) == "MEDIUM"


def test_icd_unknown_or_cross_candidate_control_is_rejected() -> None:
    candidate = next(
        item
        for item in _icd_catalog(_icd_request())[0]
        if item.candidate_type == "FOREGROUND_IP_OWNERSHIP_REVIEW"
    )
    with pytest.raises(DirectReviewError) as exc_info:
        _validate_po_control_codes(
            candidate,
            ["ADD_DATA_DELETION"],
            verdict="RISK",
        )
    assert exc_info.value.code == "RISK_CONTROL_CODE_NOT_ALLOWED"


def test_po003_our_party_duty_is_resolved_before_model_review() -> None:
    request = _po_request()
    prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    target = next(item for item in candidates if item.check_code == "PO-003")

    assert not target.requires_model_decision
    assert target.po003_precondition is not None
    assert not target.po003_precondition.counterparty_cooperation_required
    assert not target.po003_precondition.model_review_required
    assert target.candidate_id not in json.loads(prompt.split("\n", 1)[1])[
        "output_contract"
    ]["required_candidate_ids"]

    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(_po_candidate_payload(request), ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )
    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )
    decision = next(
        item
        for item in result.candidate_decisions
        if item.candidate_id == target.candidate_id
    )
    assert decision.verdict == "NO_RISK"
    assert decision.decision_source == "DETERMINISTIC_PRECONDITION"
    assert not any(item.check_code == "PO-003" for item in result.findings)


def test_po003_counterparty_cooperation_without_adverse_consequence_is_no_risk() -> None:
    request = _po_request(po003_mode="COUNTERPARTY_NO_CONSEQUENCE")
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    target = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-003"
    )

    assert target.po003_precondition is not None
    assert target.po003_precondition.counterparty_cooperation_required
    assert not target.po003_precondition.adverse_consequence_to_our_party
    assert not target.requires_model_decision


def test_po003_counterparty_dependency_with_adverse_consequence_reaches_model() -> None:
    request = _po_request(po003_mode="VALID_COUNTERPARTY_DEPENDENCY")
    prompt, ir_refs, anchor_refs = _generic_prompt(request)
    target = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-003"
    )

    assert target.po003_precondition is not None
    assert target.po003_precondition.counterparty_cooperation_required
    assert target.po003_precondition.performance_depends_on_cooperation
    assert target.po003_precondition.adverse_consequence_to_our_party
    assert target.requires_model_decision
    assert target.candidate_id in json.loads(prompt.split("\n", 1)[1])[
        "output_contract"
    ]["required_candidate_ids"]


def test_po003_absence_candidate_requires_specific_legal_absence_source() -> None:
    request = _po_request()
    projected = [
        item
        for item in request.projected_ir_items
        if item.item_id != "ir-po-cooperation"
    ]
    evidence = [
        item
        for item in request.evidence_sources
        if item.ir_item_id != "ir-po-cooperation"
    ]
    absence = [
        (
            item.model_copy(
                update={
                    "checked_scope": (
                        "已检查资料、设备、场地、接口和审批等履行必要配合；"
                        "该配合是我方履行前提，缺失会导致我方延期和违约责任"
                    ),
                    "missing_target": (
                        "缺少相对方提供资料和接口的必要配合义务、时限及顺延安排"
                    ),
                }
            )
            if item.check_code == "PO-003"
            else item
        )
        for item in request.absence_evidence_sources
    ]
    request = request.model_copy(
        update={
            "projected_ir_items": projected,
            "evidence_sources": evidence,
            "absence_evidence_sources": absence,
        }
    )
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    target = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-003"
    )

    assert target.candidate_type == "COOPERATION_OBLIGATION_ABSENT"
    assert target.po003_precondition is not None
    assert target.po003_precondition.model_review_required
    assert target.primary_evidence_source_ids[0].startswith("risk-as-")


def test_po_severity_factor_registry_covers_every_factor() -> None:
    assert set(_PO_SEVERITY_FACTOR_POLICIES) == {
        "UNILATERAL_CONTROL",
        "NO_EFFECTIVE_REMEDY",
        "BROAD_SCOPE",
        "FINANCIAL_IMPACT",
        "SCHEDULE_IMPACT",
        "OPERATIONAL_IMPACT",
        "MISSING_CORE_MECHANISM",
    }
    assert all(
        policy.allowed_check_codes
        and policy.allowed_candidate_types
        and policy.required_evidence_types
        for policy in _PO_SEVERITY_FACTOR_POLICIES.values()
    )


def test_po006_financial_factor_requires_direct_money_consequence() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidate = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-006"
    )
    accepted, rejected = _validate_po_semantic_severity_factors(
        candidate,
        ["FINANCIAL_IMPACT"],
        supporting_ids=[],
        catalog=catalog,
    )
    assert accepted == []
    assert rejected[0].reason_code == "DIRECT_CAUSAL_EVIDENCE_MISSING"

    source = catalog.evidence_sources[candidate.primary_evidence_source_ids[0]]
    supported = source.model_copy(
        update={
            "quoted_text": (
                "因相对方新增要求导致我方承担额外费用，相关价款不予调整。"
            )
        }
    )
    valid, _reason = _po_factor_has_required_evidence(
        "FINANCIAL_IMPACT",
        text_sources=[supported],
        absence_sources=[],
    )
    assert valid


def test_po006_schedule_factor_requires_causal_schedule_evidence() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidate = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-006"
    )
    accepted, rejected = _validate_po_semantic_severity_factors(
        candidate,
        ["SCHEDULE_IMPACT"],
        supporting_ids=[],
        catalog=catalog,
    )
    assert accepted == []
    assert rejected[0].reason_code == "DIRECT_CAUSAL_EVIDENCE_MISSING"

    source = catalog.evidence_sources[candidate.primary_evidence_source_ids[0]]
    supported = source.model_copy(
        update={
            "quoted_text": (
                "因相对方新增要求导致工期延期，我方仍承担逾期责任且不予顺延。"
            )
        }
    )
    valid, _reason = _po_factor_has_required_evidence(
        "SCHEDULE_IMPACT",
        text_sources=[supported],
        absence_sources=[],
    )
    assert valid


def test_po006_no_effective_remedy_requires_explicit_or_absence_evidence() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidate = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-006"
    )
    accepted, rejected = _validate_po_semantic_severity_factors(
        candidate,
        ["NO_EFFECTIVE_REMEDY"],
        supporting_ids=[],
        catalog=catalog,
    )
    assert accepted == []
    assert rejected[0].reason_code == "VALID_ABSENCE_SOURCE_MISSING"

    absence = next(
        item
        for item in request.absence_evidence_sources
        if item.check_code == "PO-006"
    ).model_copy(
        update={
            "checked_scope": "已检查变更通知、异议、复核和顺延程序",
            "missing_target": "缺少异议、复核和顺延救济程序",
        }
    )
    valid, _reason = _po_factor_has_required_evidence(
        "NO_EFFECTIVE_REMEDY",
        text_sources=[],
        absence_sources=[absence],
    )
    assert valid


def test_po006_unsupported_factor_is_audited_without_repair_or_upgrade() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-006",
        risk_candidate_type="CHANGE_CONTROL_REVIEW",
    )
    po006 = next(
        item
        for item in payload["candidate_decisions"]
        if item["candidate_id"]
        == next(
            candidate.candidate_id
            for candidate in _build_generic_candidates(
                request,
                _generic_prompt(request)[1],
                {
                    item.anchor_id: ref
                    for ref, item in _generic_prompt(request)[2].items()
                },
            )
            if candidate.check_code == "PO-006"
        )
    )
    po006["severity_factors"] = [
        "UNILATERAL_CONTROL",
        "FINANCIAL_IMPACT",
        "SCHEDULE_IMPACT",
        "NO_EFFECTIVE_REMEDY",
    ]
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )
    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )
    decision = next(
        item
        for item in result.candidate_decisions
        if item.check_code == "PO-006"
    )
    assert decision.risk_level == "MEDIUM"
    assert {
        item.factor_code for item in decision.rejected_severity_factors
    } == {
        "FINANCIAL_IMPACT",
        "SCHEDULE_IMPACT",
        "NO_EFFECTIVE_REMEDY",
    }
    assert result.rejected_severity_factor_count >= 3
    assert result.repair_count == 0


def test_stage63_acceptance_stops_after_first_failed_repetition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_calls = []

    def fail_first(_unit, runs):
        validation_calls.append(len(runs))
        return ["run 1: fixture oracle failed"]

    monkeypatch.setattr(stage63_runner, "_validate_unit_runs", fail_first)
    executed_model_runs = []
    runs: list[dict] = []
    raw_results: list[list[dict]] = []
    for run_index in range(1, 6):
        executed_model_runs.append(run_index)
        failures = stage63_runner._append_unit_run_and_validate(
            SimpleNamespace(unit_id="performance_obligations"),
            runs,
            raw_results,
            summary={"run_index": run_index},
            raw_batch_results=[{"run_index": run_index}],
        )
        if failures:
            break

    assert executed_model_runs == [1]
    assert validation_calls == [1]
    assert len(runs) == 1
    assert len(raw_results) == 1


def test_stage63_summary_resolves_absence_by_scope_not_finding_check() -> None:
    source_id = "risk-as-" + "9" * 32
    evidence = EvidenceCandidate(
        evidence_local_id="evidence-" + "8" * 32,
        finding_local_id="finding-" + "7" * 32,
        evidence_type="ABSENCE",
        checked_scope="责任上限、赔偿范围和例外条款",
        verification_note="Python确定性扫描未发现责任上限",
    )

    assert stage63_runner._summary_evidence_source_id(
        evidence,
        source_by_binding={},
        absence_by_scope={
            (
                "责任上限、赔偿范围和例外条款",
                "Python确定性扫描未发现责任上限",
            ): source_id
        },
    ) == source_id


def test_po_stability_uses_root_and_primary_evidence_not_supporting_variation() -> None:
    stable_root = [
        "PO-006",
        "CHANGE_CONTROL_REVIEW",
        "MEDIUM",
        ["candidate-po006"],
        ["primary-po006"],
    ]
    runs = [
        {
            "canonical_root_stability_keys": [stable_root],
            "canonical_source_risk_keys": [
                ["PO-006", "CHANGE_CONTROL_RISK", "MEDIUM", ["primary-po006"]]
            ],
        },
        {
            "canonical_root_stability_keys": [stable_root],
            "canonical_source_risk_keys": [
                [
                    "PO-006",
                    "CHANGE_CONTROL_RISK",
                    "MEDIUM",
                    ["primary-po006", "supporting-po006"],
                ]
            ],
        },
    ]

    values = stage63_runner._canonical_risk_stability_sets(
        "performance_obligations",
        runs,
    )

    assert values[0] == values[1]


class FakeRuntime:
    def __init__(self, responses: list[LlmCompletionResult]) -> None:
        self.responses = responses
        self.calls = []

    async def complete_with_usage(self, **kwargs) -> LlmCompletionResult:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_generic_direct_review_maps_real_anchor_and_uses_zero_tools() -> None:
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(_payload(), ensure_ascii=False),
                "formation_validity_authority",
            )
        ]
    )
    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            framework_run_id="run-base-1",
        )
    )

    assert [item.check_code for item in result.check_results] == list(FVA_CODES)
    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.tool_call_count == 0
    assert result.reason_code_enrichment_count == 5
    assert result.fva_assessments[0].assessment_type == "NO_VISIBLE_ISSUE"
    assert result.fva_assessments[0].external_verification_required is False
    assert result.findings[0].source_unit_id == "formation_validity_authority"
    evidence = result.findings[0].evidence_candidates[0]
    assert evidence.source_ir_item_id == "ir-party-1"
    assert evidence.anchor_id == "anchor-party-1"
    assert evidence.block_id == "block-party-1"
    assert runtime.calls[0]["temperature"] == 0
    assert runtime.calls[0]["thinking_override"] is False
    assert runtime.calls[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in runtime.calls[0]


def test_generic_prompt_has_deterministic_candidates_and_no_technical_output_fields() -> None:
    text, ir_refs, anchor_refs = _generic_prompt(_request())
    payload = json.loads(text.split("\n", 1)[1])
    candidates = _build_generic_candidates(
        _request(),
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )

    assert len(candidates) == 5
    assert all(item.requires_model_decision for item in candidates)
    assert candidates[0].candidate_ir_refs == ["I001"]
    assert candidates[0].candidate_evidence_refs == ["A001"]
    assert any(
        "reason_code由Python生成" in item
        for item in payload["output_contract"]["rules"]
    )
    assert any(
        "FVA-002缺少外部材料只能判外部核验" in item
        for item in payload["output_contract"]["rules"]
    )
    assert set(payload["output_contract"]["fva002_state_contract"]) == {
        "TEXTUAL_AUTHORITY_RISK",
        "EXTERNAL_VERIFICATION_REQUIRED",
        "NO_VISIBLE_ISSUE",
    }


def test_po_candidates_cover_frozen_business_hypotheses() -> None:
    request = _po_request()
    text, ir_refs, anchor_refs = _generic_prompt(request)
    payload = json.loads(text.split("\n", 1)[1])
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )

    assert {item.check_code for item in candidates} == {
        f"PO-{index:03d}" for index in range(1, 8)
    }
    assert {item.candidate_type for item in candidates}.issuperset(
        {
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "COOPERATION_DEPENDENCY",
            "QUALITY_STANDARD_UNMEASURABLE",
            "ACCEPTANCE_MECHANISM_REVIEW",
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "CHANGE_CONTROL_REVIEW",
            "WARRANTY_SUPPORT_REVIEW",
        }
    )
    assert all(item.trigger_reason for item in candidates)
    assert len({item.candidate_id for item in candidates}) == len(candidates)
    resolved = [item for item in candidates if not item.requires_model_decision]
    assert len(resolved) == 1
    assert resolved[0].check_code == "PO-003"
    assert resolved[0].po003_precondition is not None
    assert all(
        ref in ir_refs
        for item in candidates
        for ref in item.candidate_ir_refs
    )
    assert all(
        ref in anchor_refs
        for item in candidates
        for ref in item.candidate_evidence_refs
    )
    assert "deterministic_candidate_legend" not in payload
    assert "deterministic_candidates" not in payload
    assert "evidence_sources" not in payload
    assert all(
        item["allowed_evidence_sources"]
        or item["allowed_absence_sources"]
        for item in payload["assigned_checks"]
    )
    assert "projected_ir" not in payload
    assert "source_excerpts" not in payload
    assert any(
        "Primary Evidence" in item
        for item in payload["output_contract"]["rules"]
    )
    assert payload["review_context"]["party_a"] == "甲方教育科技有限公司"
    assert payload["review_context"]["party_b"] == "乙方人工智能科技有限公司"
    assert payload["review_context"]["our_contract_role"] == "乙方"
    assert any(
        "每个required_candidate_id必须返回一次且仅一次" in item
        for item in payload["output_contract"]["rules"]
    )
    assert payload["output_contract"]["candidate_decision_fields"] == [
        "candidate_id",
        "verdict",
        "decision_summary",
        "severity_factors",
        "supporting_evidence_source_ids",
        "counter_evidence_source_ids",
        "recommended_control_codes",
    ]
    assert "risk_only_fields" not in payload["output_contract"]
    assert payload["candidate_legend"][0] == "candidate_id"
    assert all(
        len(candidate) == len(payload["candidate_legend"])
        for check in payload["assigned_checks"]
        for candidate in check["candidates"]
    )
    assert all(
        "allowed_risk_types" not in item
        for item in payload["assigned_checks"]
    )
    assert not any(
        item.candidate_type
        in {"PAYMENT", "INVOICE", "TAX", "AUTHORITY", "INTELLECTUAL_PROPERTY"}
        for item in candidates
    )


def test_po_directional_candidates_follow_selected_review_side() -> None:
    party_a_request = _po_request(perspective="PARTY_A")
    _text, ir_refs, anchor_refs = _generic_prompt(party_a_request)
    party_a_candidates = _build_generic_candidates(
        party_a_request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )

    assert not {
        "SCOPE_EXPANSION",
        "RIGHTS_OBLIGATIONS_IMBALANCE",
        "CHANGE_CONTROL_REVIEW",
    } & {item.candidate_type for item in party_a_candidates}
    assert "DELIVERY_SCHEDULE_REVIEW" in {
        item.candidate_type for item in party_a_candidates
    }


def test_po_prompt_isolates_sources_by_check_code() -> None:
    request = _po_request()
    text, _ir_refs, _anchor_refs = _generic_prompt(request)
    payload = json.loads(text.split("\n", 1)[1])
    prompt_checks = {
        item["check_code"]: item for item in payload["assigned_checks"]
    }
    source_ids_by_check = {
        code: {
            source[0]
            for source in check["allowed_evidence_sources"]
        }
        for code, check in prompt_checks.items()
    }
    catalog, _ir_refs, _anchor_refs = _po_catalog(request)

    assert source_ids_by_check == {
        code: set(source_ids)
        for code, source_ids in catalog.allowed_source_ids_by_check.items()
        if code in source_ids_by_check
    }
    shared = next(
        source
        for source in request.evidence_sources
        if len(source.allowed_check_codes) > 1
    )
    for check_code in shared.allowed_check_codes:
        assert shared.source_id in source_ids_by_check[check_code]
    for check_code in set(source_ids_by_check) - set(shared.allowed_check_codes):
        assert shared.source_id not in source_ids_by_check[check_code]


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_fixed_fixture_po006_source_is_hidden_from_po002_prompt() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    context = next(
        item
        for item in plan.contexts
        if item.unit_id == "performance_obligations"
        and {spec.check_code for spec in item.check_specs}
        == {"PO-002", "PO-005", "PO-006"}
    )
    request = generic_request_from_context(context)
    target = next(
        source
        for source in request.evidence_sources
        if source.quoted_text.startswith(
            "为满足本项目目的，甲方在履行过程中提出的要求"
        )
    )
    text, _ir_refs, _anchor_refs = _generic_prompt(request)
    prompt_checks = {
        item["check_code"]: item
        for item in json.loads(text.split("\n", 1)[1])["assigned_checks"]
    }
    source_ids_by_check = {
        code: {
            source[0]
            for source in check["allowed_evidence_sources"]
        }
        for code, check in prompt_checks.items()
    }

    assert target.allowed_check_codes == ["PO-006"]
    assert target.source_id in source_ids_by_check["PO-006"]
    assert target.source_id not in source_ids_by_check["PO-002"]


def test_po_candidate_ids_are_stable_and_generation_scoped() -> None:
    request = _po_request()
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    anchor_ref_by_id = {
        item.anchor_id: ref for ref, item in anchor_refs.items()
    }
    first = _build_generic_candidates(request, ir_refs, anchor_ref_by_id)
    second = _build_generic_candidates(request, ir_refs, anchor_ref_by_id)
    other_generation = request.model_copy(
        update={"generation_id": "generation-po-2"}
    )
    third = _build_generic_candidates(
        other_generation,
        ir_refs,
        anchor_ref_by_id,
    )

    assert first == second
    assert [item.candidate_id for item in first] != [
        item.candidate_id for item in third
    ]
    assert all(item.primary_evidence_source_ids for item in first)
    assert all(
        len(item.primary_evidence_source_ids)
        == len(set(item.primary_evidence_source_ids))
        for item in first
    )


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("missing", "RISK_CANDIDATE_COVERAGE_INVALID"),
        ("duplicate", "RISK_CANDIDATE_DECISION_DUPLICATED"),
        ("unknown", "RISK_CANDIDATE_UNKNOWN"),
    ],
)
def test_po_candidate_coverage_is_a_hard_gate(mutation, error_code) -> None:
    request = _po_request()
    payload = _po_candidate_payload(request)
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    model_candidates = [
        item for item in candidates if item.requires_model_decision
    ]
    expected_ids = tuple(item.candidate_id for item in model_candidates)
    if mutation == "missing":
        payload["candidate_decisions"].pop()
    elif mutation == "duplicate":
        payload["candidate_decisions"][1]["candidate_id"] = (
            payload["candidate_decisions"][0]["candidate_id"]
        )
    else:
        payload["candidate_decisions"][0]["candidate_id"] = (
            "risk-candidate-" + "f" * 32
        )

    with pytest.raises(DirectReviewError) as raised:
        _parse_po_candidate_output(
            json.dumps(payload, ensure_ascii=False),
            expected_ids=expected_ids,
            candidates_by_id={
                item.candidate_id: item for item in model_candidates
            },
        )
    assert raised.value.code == error_code


@pytest.mark.parametrize(
    ("check_code", "candidate_strength"),
    [
        ("PO-004", "HARD_RULE"),
        ("PO-002", "STRONG_SIGNAL"),
    ],
)
def test_po_strong_negative_decision_requires_counter_evidence(
    check_code,
    candidate_strength,
) -> None:
    request = _po_request()
    payload = _po_candidate_payload(request)
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    target = next(
        item
        for item in candidates
        if item.check_code == check_code
        and item.candidate_strength == candidate_strength
    )
    decision = next(
        item
        for item in payload["candidate_decisions"]
        if item["candidate_id"] == target.candidate_id
    )
    decision["verdict"] = "NO_RISK"
    decision["decision_summary"] = "未引用任何能够推翻候选的反向证据。"
    decision["recommended_control_codes"] = []
    decision["counter_evidence_source_ids"] = []
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(
                runtime_factory=lambda _tenant: runtime
            ).review(
                request,
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )
    assert raised.value.code == "RISK_NEGATIVE_DECISION_UNSUPPORTED"
    assert len(runtime.calls) == 1


def test_po_semantic_no_risk_is_explicit_and_creates_no_finding() -> None:
    request = _po_request()
    payload = _po_candidate_payload(request)
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    assert len(result.candidate_decisions) == (
        len(payload["candidate_decisions"]) + 1
    )
    assert any(
        item.check_code == "PO-003"
        and item.decision_source == "DETERMINISTIC_PRECONDITION"
        for item in result.candidate_decisions
    )
    target = next(
        item for item in result.candidate_decisions if item.check_code == "PO-005"
    )
    assert target.verdict == "NO_RISK"
    assert not any(item.check_code == "PO-005" for item in result.findings)
    assert next(
        item for item in result.check_decisions if item.check_code == "PO-005"
    ).reason_code == "NO_RISK_IDENTIFIED"


def test_po_risk_materialization_and_severity_are_deterministic() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-006",
        risk_candidate_type="CHANGE_CONTROL_REVIEW",
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )
    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )
    decision = next(
        item
        for item in result.candidate_decisions
        if item.check_code == "PO-006" and item.verdict == "RISK"
    )
    finding = next(item for item in result.findings if item.check_code == "PO-006")

    assert decision.risk_level == "MEDIUM"
    assert finding.risk_level == "MEDIUM"
    assert finding.risk_type == "PO-006_RISK"
    assert decision.primary_evidence_source_ids
    assert {
        item.source_ir_item_id
        for item in finding.evidence_candidates
        if item.source_ir_item_id is not None
    }
    assert (
        _po_risk_level(
            next(
                item
                for item in _build_generic_candidates(
                    request,
                    _generic_prompt(request)[1],
                    {
                        excerpt.anchor_id: ref
                        for ref, excerpt in _generic_prompt(request)[2].items()
                    },
                )
                if item.candidate_id == decision.candidate_id
            ),
            decision.severity_factors,
        )
        == "MEDIUM"
    )


def test_po_shared_core_candidates_materialize_one_stable_root_and_finding() -> None:
    request = _po_request(
        shared_po001_anchor=True,
        extra_po001_support=True,
        delivery_object="项目交付，并应在10个工作日内完成",
    )
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-001",
        risk_candidate_types={
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
        },
    )
    _use_minimal_po001_root_factors(request, payload)
    for decision in payload["candidate_decisions"]:
        if decision["verdict"] == "RISK":
            decision["severity_factors"] = []

    def execute():
        runtime = FakeRuntime(
            [
                _completion(
                    json.dumps(payload, ensure_ascii=False),
                    "performance_obligations",
                )
            ]
        )
        return asyncio.run(
            GenericBaseDirectReviewer(
                runtime_factory=lambda _tenant: runtime
            ).review(
                request,
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    first = execute()
    second = execute()
    candidate_decisions = [
        item
        for item in first.candidate_decisions
        if item.check_code == "PO-001" and item.verdict == "RISK"
    ]
    roots = [
        item
        for item in first.canonical_risk_roots
        if item.check_code == "PO-001"
    ]
    findings = [item for item in first.findings if item.check_code == "PO-001"]

    assert len(candidate_decisions) == 2
    assert {item.risk_level for item in candidate_decisions} == {"MEDIUM"}
    assert len(roots) == 1
    assert len(findings) == 1
    root = roots[0]
    assert root.root_type == "CORE_SCOPE_AND_DELIVERY_IMBALANCE"
    assert root.risk_level == "HIGH"
    assert root.severity_factors.unilateral_control
    assert root.severity_factors.broad_scope
    assert root.severity_factors.schedule_impact
    assert root.source_candidate_ids == [
        item.candidate_id for item in candidate_decisions
    ]
    assert len(root.primary_evidence_source_ids) == len(
        set(root.primary_evidence_source_ids)
    )
    assert len(root.supporting_evidence_source_ids) == len(
        set(root.supporting_evidence_source_ids)
    )
    assert len(root.recommended_control_codes) == len(
        set(root.recommended_control_codes)
    )
    assert root.finding_local_id == findings[0].finding_local_id
    assert second.canonical_risk_roots == first.canonical_risk_roots
    assert second.findings == first.findings
    po001 = next(
        item for item in first.check_decisions if item.check_code == "PO-001"
    )
    assert po001.finding_local_ids == [root.finding_local_id]


def test_po_delivery_schedule_factor_requires_literal_core_evidence() -> None:
    scheduled = _po_request(
        delivery_object="项目交付，并应在10个工作日内完成",
    )
    unscheduled = _po_request(delivery_object="项目交付")

    def delivery_candidate(request: GenericReviewRequest):
        _prompt, ir_refs, anchor_refs = _generic_prompt(request)
        return next(
            item
            for item in _build_generic_candidates(
                request,
                ir_refs,
                {item.anchor_id: ref for ref, item in anchor_refs.items()},
            )
            if item.candidate_type == "DELIVERY_SCHEDULE_REVIEW"
        )

    assert (
        "SCHEDULE_IMPACT"
        in delivery_candidate(scheduled).deterministic_severity_factors
    )
    assert (
        "SCHEDULE_IMPACT"
        not in delivery_candidate(unscheduled).deterministic_severity_factors
    )


def test_po_independent_scope_and_delivery_candidates_remain_two_roots() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-001",
        risk_candidate_types={
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
        },
    )
    _use_minimal_po001_root_factors(request, payload)
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )
    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    roots = [
        item
        for item in result.canonical_risk_roots
        if item.check_code == "PO-001"
    ]
    findings = [item for item in result.findings if item.check_code == "PO-001"]
    risk_decisions = [
        item
        for item in result.candidate_decisions
        if item.check_code == "PO-001" and item.verdict == "RISK"
    ]
    assert [item.candidate_type for item in risk_decisions] == [
        "SCOPE_EXPANSION",
        "DELIVERY_SCHEDULE_REVIEW",
    ]
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    core_anchors = [
        {
            catalog.evidence_sources[source_id].anchor_id
            for source_id in candidate.core_primary_evidence_source_ids
            if source_id in catalog.evidence_sources
        }
        for candidate in candidates
        if candidate.check_code == "PO-001"
    ]
    assert core_anchors[0].isdisjoint(core_anchors[1]), core_anchors
    assert len(roots) == 2
    assert len(findings) == 2
    assert all(len(item.source_candidate_ids) == 1 for item in roots)


def test_po_root_merge_requires_explicit_reciprocal_registry_permission() -> None:
    request = _po_request(shared_po001_anchor=True)
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    scope = next(
        item for item in candidates if item.candidate_type == "SCOPE_EXPANSION"
    )
    delivery = next(
        item
        for item in candidates
        if item.candidate_type == "DELIVERY_SCHEDULE_REVIEW"
    )
    specs = {item.check_code: item for item in request.assigned_check_specs}

    assert _po_candidates_share_canonical_root(
        scope,
        delivery,
        specs=specs,
        catalog=catalog,
    )
    blocked = delivery.model_copy(
        update={"merge_compatible_candidate_types": []}
    )
    assert not _po_candidates_share_canonical_root(
        scope,
        blocked,
        specs=specs,
        catalog=catalog,
    )


def test_po_supporting_evidence_change_does_not_change_root_identity() -> None:
    request = _po_request(shared_po001_anchor=True)
    base_payload = _po_candidate_payload(
        request,
        risk_check_code="PO-001",
        risk_candidate_types={
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
        },
    )
    _use_minimal_po001_root_factors(request, base_payload)
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(base_payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )
    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    catalog = _po_evidence_catalog(
        request,
        candidates,
        ir_refs,
        anchor_refs,
    )
    specs = {item.check_code: item for item in request.assigned_check_specs}
    base_decisions = {
        item.candidate_id: item for item in result.candidate_decisions
    }
    target_id = next(
        item.candidate_id
        for item in candidates
        if item.candidate_type == "DELIVERY_SCHEDULE_REVIEW"
    )
    changed_decisions = dict(base_decisions)
    changed_decisions[target_id] = base_decisions[target_id].model_copy(
        update={"supporting_evidence_source_ids": ["risk-es-context-only"]}
    )
    base_groups = _po_canonical_root_groups(
        candidates,
        base_decisions,
        specs,
        catalog,
    )
    changed_groups = _po_canonical_root_groups(
        candidates,
        changed_decisions,
        specs,
        catalog,
    )
    assert [
        [item.candidate_id for item in group] for group in changed_groups
    ] == [
        [item.candidate_id for item in group] for group in base_groups
    ]


def test_po_primary_repeated_as_supporting_is_deduplicated_without_repair() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-006",
        risk_candidate_type="CHANGE_CONTROL_REVIEW",
    )
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    target = next(
        item
        for item in candidates
        if item.check_code == "PO-006"
        and item.candidate_type == "CHANGE_CONTROL_REVIEW"
    )
    raw = next(
        item
        for item in payload["candidate_decisions"]
        if item["candidate_id"] == target.candidate_id
    )
    raw["supporting_evidence_source_ids"] = [
        target.primary_evidence_source_ids[0]
    ]
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    decision = next(
        item
        for item in result.candidate_decisions
        if item.candidate_id == target.candidate_id
    )
    assert result.supporting_primary_overlap_count == 1
    assert decision.supporting_evidence_source_ids == []
    assert decision.primary_evidence_source_ids == target.primary_evidence_source_ids
    assert result.repair_count == 0


def test_po_model_schema_excludes_party_primary_and_formal_finding_fields() -> None:
    request = _po_request()
    payload = _po_candidate_payload(request)
    raw = payload["candidate_decisions"][0]
    forbidden = {
        "perspective": "PARTY_B",
        "our_party": request.counterparty,
        "counterparty": request.our_party,
        "primary_evidence_source_ids": ["risk-es-" + "a" * 32],
        "risk_level": "HIGH",
        "title": "模型标题",
        "issue": "模型问题",
        "impact_to_our_party": "模型影响",
        "suggestion": "模型建议",
    }
    for field, value in forbidden.items():
        candidate = json.loads(json.dumps(payload, ensure_ascii=False))
        candidate["candidate_decisions"][0][field] = value
        with pytest.raises(Exception):
            CandidateDecisionResponseRaw.model_validate(candidate)


def test_po_wrong_party_summary_is_audited_but_cannot_pollute_final_finding() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-006",
        risk_candidate_type="CHANGE_CONTROL_REVIEW",
    )
    target = next(
        item
        for item in payload["candidate_decisions"]
        if item["verdict"] == "RISK"
        and "UNILATERAL_CONTROL" in item["severity_factors"]
    )
    target["decision_summary"] = "我方作为甲方受到乙方单方变更安排影响。"
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    finding = next(item for item in result.findings if item.check_code == "PO-006")
    formal_text = "".join(
        (
            finding.title,
            finding.issue,
            finding.impact_to_our_party,
            finding.suggestion,
        )
    )
    assert result.decision_summary_perspective_warning_count >= 1
    assert "我方作为甲方" not in formal_text
    assert request.our_party in finding.impact_to_our_party
    assert finding.perspective == request.perspective
    assert finding.our_party == request.our_party
    assert finding.counterparty == request.counterparty


def test_po_control_code_materializes_suggestion_without_domain_leakage() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-005",
        risk_candidate_type="ASSIGNMENT_SUBCONTRACT_REVIEW",
    )
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidate = next(
        item
        for item in _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        if item.check_code == "PO-005"
        and item.candidate_type == "ASSIGNMENT_SUBCONTRACT_REVIEW"
    )
    target = next(
        item
        for item in payload["candidate_decisions"]
        if item["candidate_id"] == candidate.candidate_id
    )
    target["recommended_control_codes"] = ["ADD_CONFIDENTIALITY_GUARD"]
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    finding = next(item for item in result.findings if item.check_code == "PO-005")
    assert "保密" in finding.suggestion
    assert finding.category == "RIGHTS_OBLIGATIONS_IMBALANCE"
    assert finding.risk_type == "PO-005_RISK"


def test_po_unknown_or_cross_candidate_control_code_is_rejected() -> None:
    request = _po_request()
    payload = _po_candidate_payload(
        request,
        risk_check_code="PO-006",
        risk_candidate_type="CHANGE_CONTROL_REVIEW",
    )
    target = next(
        item
        for item in payload["candidate_decisions"]
        if item["verdict"] == "RISK"
        and "UNILATERAL_CONTROL" in item["severity_factors"]
    )
    target["recommended_control_codes"] = ["DEFINE_ACCEPTANCE_PERIOD"]
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(
                runtime_factory=lambda _tenant: runtime
            ).review(
                request,
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )
    assert raised.value.code == "RISK_CONTROL_CODE_NOT_ALLOWED"
    assert len(runtime.calls) == 1


def test_po_insufficient_evidence_is_scoped_to_candidate_and_check() -> None:
    request = _po_request()
    payload = _po_candidate_payload(request)
    target = payload["candidate_decisions"][0]
    target["verdict"] = "INSUFFICIENT_EVIDENCE"
    target["decision_summary"] = "缺少能够完成该候选判断的条款上下文。"
    target["counter_evidence_source_ids"] = []
    target["recommended_control_codes"] = []
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(
            runtime_factory=lambda _tenant: runtime
        ).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    decision = next(
        item
        for item in result.candidate_decisions
        if item.candidate_id == target["candidate_id"]
    )
    check = next(
        item
        for item in result.check_results
        if item.check_code == decision.check_code
    )
    assert result.status == "PARTIAL_FAILED"
    assert decision.verdict == "INSUFFICIENT_EVIDENCE"
    assert decision.reason_code == "INSUFFICIENT_EVIDENCE"
    assert check.status == "REVIEWED"
    assert check.reason_code == "INSUFFICIENT_EVIDENCE"
    assert not any(
        target["candidate_id"] in item.source_candidate_ids
        for item in result.canonical_risk_roots
    )


def test_po_missing_acceptance_generates_absence_candidate() -> None:
    request = _po_request()
    request = request.model_copy(
        update={
            "projected_ir_items": [
                item
                for item in request.projected_ir_items
                if item.ir_type != "acceptance_terms"
            ],
            "present_ir_types": [
                item
                for item in request.present_ir_types
                if item != "acceptance_terms"
            ],
            "missing_ir_types": sorted(
                {*request.missing_ir_types, "acceptance_terms"}
            ),
        }
    )
    _text, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )

    assert any(
        item.check_code == "PO-004"
        and item.candidate_type == "ACCEPTANCE_MECHANISM_ABSENT"
        and not item.candidate_ir_refs
        and not item.candidate_evidence_refs
        for item in candidates
    )


def test_po_evidence_source_id_is_stable_and_prebound() -> None:
    first = _po_request()
    second = _po_request()

    assert first.evidence_sources == second.evidence_sources
    assert len({item.source_id for item in first.evidence_sources}) == len(
        first.evidence_sources
    )
    for source in first.evidence_sources:
        item = next(
            item
            for item in first.projected_ir_items
            if item.item_id == source.ir_item_id
        )
        excerpt = next(
            item
            for item in first.source_excerpts
            if item.anchor_id == source.anchor_id
        )
        assert source.anchor_id in {
            anchor.anchor_id for anchor in item.source_anchors
        }
        assert source.block_id == excerpt.block_id
        assert source.quoted_text == excerpt.quoted_text
        assert source.quoted_text_hash == excerpt.quoted_text_hash


def test_po_rejects_cross_generation_source_and_invalid_source_hash() -> None:
    request = _po_request()
    payload = request.model_dump(mode="json")
    payload["evidence_sources"][0]["generation_id"] = "generation-other"
    with pytest.raises(Exception):
        GenericReviewRequest.model_validate(payload)

    payload = request.model_dump(mode="json")
    payload["evidence_sources"][0]["quoted_text_hash"] = "sha256:" + "0" * 64
    with pytest.raises(Exception):
        GenericReviewRequest.model_validate(payload)


def test_po_model_cannot_output_python_technical_evidence_fields() -> None:
    with pytest.raises(Exception):
        GenericModelFindingDraft.model_validate(
            {
                "check_code": "PO-003",
                "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
                "risk_type": "PO-003_RISK",
                "risk_level": "MEDIUM",
                "title": "配合义务不完整",
                "issue": "配合义务和延迟边界不完整。",
                "impact_to_our_party": "我方可能承担不可控延迟。",
                "suggestion": "明确配合时限和顺延后果。",
                "evidence_source_ids": ["risk-es-" + "1" * 32],
                "anchor_id": "model-must-not-write-anchor",
            }
        )


def test_po_source_backed_finding_derives_ir_anchor_and_deduplicates() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    source_id = catalog.allowed_source_ids_by_check["PO-003"][0]
    draft = GenericModelFindingDraft.model_validate(
        {
            "check_code": "PO-003",
            "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "risk_type": "PO-003_RISK",
            "risk_level": "MEDIUM",
            "title": "配合义务不完整",
            "issue": "配合义务和延迟边界不完整。",
            "impact_to_our_party": "我方可能承担不可控延迟。",
            "suggestion": "明确配合时限和顺延后果。",
            "evidence_source_ids": [source_id, source_id],
        }
    )

    selected, normalized, ignored = _resolve_po_evidence_source_ids(
        draft,
        catalog,
        ir_refs,
        anchor_refs,
    )

    assert selected == [source_id]
    assert normalized == 1
    assert ignored == 0
    source = catalog.evidence_sources[source_id]
    assert source.ir_item_id
    assert source.anchor_id


def test_po_unknown_and_cross_check_source_are_rejected() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    po003_allowed = set(catalog.allowed_source_ids_by_check["PO-003"])
    other_source = next(
        source_id
        for check_code, source_ids in catalog.allowed_source_ids_by_check.items()
        if check_code != "PO-003"
        for source_id in source_ids
        if source_id not in po003_allowed
    )
    for source_id in ("risk-es-" + "f" * 32, other_source):
        draft = GenericModelFindingDraft.model_validate(
            {
                "check_code": "PO-003",
                "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
                "risk_type": "PO-003_RISK",
                "risk_level": "MEDIUM",
                "title": "配合义务不完整",
                "issue": "配合义务和延迟边界不完整。",
                "impact_to_our_party": "我方可能承担不可控延迟。",
                "suggestion": "明确配合时限和顺延后果。",
                "evidence_source_ids": [source_id],
            }
        )
        with pytest.raises(DirectReviewError) as raised:
            _resolve_po_evidence_source_ids(
                draft,
                catalog,
                ir_refs,
                anchor_refs,
            )
        assert raised.value.code == "RISK_EVIDENCE_SOURCE_NOT_ALLOWED"


def test_po_legacy_ir_anchor_pair_uses_only_strict_unique_normalization() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    source_id = catalog.allowed_source_ids_by_check["PO-003"][0]
    source = catalog.evidence_sources[source_id]
    ir_ref = next(
        ref for ref, item in ir_refs.items() if item.item_id == source.ir_item_id
    )
    invalid_evidence_ref = next(
        ref
        for ref, excerpt in anchor_refs.items()
        if excerpt.anchor_id != source.anchor_id
    )
    draft = GenericModelFindingDraft.model_validate(
        {
            "check_code": "PO-003",
            "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "risk_type": "PO-003_RISK",
            "risk_level": "MEDIUM",
            "title": "配合义务不完整",
            "issue": "配合义务和延迟边界不完整。",
            "impact_to_our_party": "我方可能承担不可控延迟。",
            "suggestion": "明确配合时限和顺延后果。",
            "evidence": [
                {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": ir_ref,
                    "evidence_ref": invalid_evidence_ref,
                }
            ],
        }
    )

    selected, normalized, ignored = _resolve_po_evidence_source_ids(
        draft,
        catalog,
        ir_refs,
        anchor_refs,
    )

    assert selected == [source_id]
    assert normalized == 1
    assert ignored == 2


def test_po_absence_source_is_python_defined() -> None:
    request = _po_request()
    request = request.model_copy(
        update={
            "projected_ir_items": [
                item
                for item in request.projected_ir_items
                if item.ir_type != "acceptance_terms"
            ],
            "present_ir_types": [
                item
                for item in request.present_ir_types
                if item != "acceptance_terms"
            ],
            "missing_ir_types": sorted(
                {*request.missing_ir_types, "acceptance_terms"}
            ),
        }
    )
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    source_id = next(iter(catalog.absence_sources))
    source = catalog.absence_sources[source_id]
    draft = GenericModelFindingDraft.model_validate(
        {
            "check_code": source.check_code,
            "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "risk_type": f"{source.check_code}_RISK",
            "risk_level": "MEDIUM",
            "title": "验收机制缺失",
            "issue": "合同没有形成完整验收机制。",
            "impact_to_our_party": "交付完成标准不确定。",
            "suggestion": "补充标准、期限、程序和复验。",
            "evidence_source_ids": [source_id],
        }
    )

    selected, normalized, ignored = _resolve_po_evidence_source_ids(
        draft,
        catalog,
        ir_refs,
        anchor_refs,
    )

    assert selected == [source_id]
    assert normalized == 0
    assert ignored == 0
    assert source.checked_scope
    assert source.verification_method


def test_po_direct_reviewer_uses_only_source_id_and_derives_final_evidence() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    target = next(
        item
        for item in candidates
        if item.check_code == "PO-004"
        and item.candidate_type == "QUALITY_STANDARD_UNMEASURABLE"
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(
                    _po_candidate_payload(
                        request,
                        risk_check_code="PO-004",
                        risk_candidate_type=target.candidate_type,
                    ),
                    ensure_ascii=False,
                ),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(
            runtime_factory=lambda _tenant: runtime
        ).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            framework_run_id="run-po-1",
        )
    )

    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.evidence_selection_repair_count == 0
    assert result.evidence_binding_normalization_count == 0
    assert set(target.primary_evidence_source_ids).issubset(
        result.selected_evidence_source_ids
    )
    finding = next(item for item in result.findings if item.check_code == "PO-004")
    evidence = finding.evidence_candidates[0]
    source = catalog.evidence_sources[target.primary_evidence_source_ids[0]]
    assert evidence.source_ir_item_id == source.ir_item_id
    assert evidence.anchor_id == source.anchor_id
    assert evidence.block_id == source.block_id
    assert evidence.quoted_text == source.quoted_text
    assert evidence.quoted_text_hash == source.quoted_text_hash
    assert runtime.calls[0]["temperature"] == 0
    assert runtime.calls[0]["thinking_override"] is False
    assert "tools" not in runtime.calls[0]


def test_po_technical_fields_and_primary_evidence_are_deterministic() -> None:
    request = _po_request()
    catalog, ir_refs, anchor_refs = _po_catalog(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    target = next(
        item
        for item in candidates
        if item.check_code == "PO-004"
        and item.candidate_type == "QUALITY_STANDARD_UNMEASURABLE"
    )
    business_payload = _po_candidate_payload(
        request,
        risk_check_code="PO-004",
        risk_candidate_type=target.candidate_type,
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(business_payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.schema_repair_count == 0
    finding = next(item for item in result.findings if item.check_code == "PO-004")
    assert finding.check_code == "PO-004"
    assert finding.category == "RIGHTS_OBLIGATIONS_IMBALANCE"
    assert finding.risk_type == "PO-004_RISK"
    decision = next(
        item
        for item in result.candidate_decisions
        if item.candidate_id == target.candidate_id
    )
    assert decision.primary_evidence_source_ids == target.primary_evidence_source_ids
    assert {
        item.source_id
        for item in catalog.evidence_sources.values()
        if item.source_id in target.primary_evidence_source_ids
    } == set(target.primary_evidence_source_ids)


@pytest.mark.skipif(
    not os.getenv(FIXTURE_ENV) or not os.getenv(FAILED_PO_ATTEMPT_ENV),
    reason="fixed risk fixture or failed PO Attempt Artifact is not configured",
)
def test_legacy_po_check_level_output_is_rejected_without_candidate_repair() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    artifact = json.loads(
        Path(os.environ[FAILED_PO_ATTEMPT_ENV]).read_text(encoding="utf-8")
    )
    initial = next(
        item
        for item in artifact["attempts"]
        if item["batch_id"] == "risk-batch-912122916f35b00119327feb5893c68d"
        and item["attempt_type"] == "INITIAL"
    )
    context = next(
        item
        for item in plan.contexts
        if item.batch_id == initial["batch_id"]
    )
    runtime = FakeRuntime(
        [
            _completion(
                initial["raw_response"],
                "performance_obligations",
            )
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                generic_request_from_context(context),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
                allow_evidence_selection_repair=False,
            )
        )
    assert raised.value.code == "RISK_DIRECT_SCHEMA_INVALID"
    assert len(runtime.calls) == 1


@pytest.mark.skipif(
    not os.getenv(FIXTURE_ENV) or not os.getenv(CANDIDATE_PO_ATTEMPT_ENV),
    reason="fixed risk fixture or Candidate PO Attempt Artifact is not configured",
)
def test_latest_candidate_failure_replays_with_deterministic_roles_and_text() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    artifact = json.loads(
        Path(os.environ[CANDIDATE_PO_ATTEMPT_ENV]).read_text(encoding="utf-8")
    )
    initial = artifact["attempts"][0]
    context = next(
        item for item in plan.contexts if item.batch_id == initial["batch_id"]
    )
    request = generic_request_from_context(context)
    _prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    candidates_by_id = {item.candidate_id: item for item in candidates}
    old = json.loads(initial["raw_response"])
    factor_map = {
        "unilateral_control": "UNILATERAL_CONTROL",
        "no_effective_remedy": "NO_EFFECTIVE_REMEDY",
        "broad_scope": "BROAD_SCOPE",
        "financial_impact": "FINANCIAL_IMPACT",
        "schedule_impact": "SCHEDULE_IMPACT",
        "operational_impact": "OPERATIONAL_IMPACT",
        "missing_core_mechanism": "MISSING_CORE_MECHANISM",
    }
    replay = {"candidate_decisions": []}
    for item in old["candidate_decisions"]:
        candidate = candidates_by_id[item["candidate_id"]]
        replay["candidate_decisions"].append(
            {
                "candidate_id": item["candidate_id"],
                "verdict": item["verdict"],
                "decision_summary": item["decision_note"],
                "severity_factors": [
                    code
                    for field, code in factor_map.items()
                    if item["severity_factors"][field]
                ],
                "supporting_evidence_source_ids": (
                    item["supporting_evidence_source_ids"]
                ),
                "counter_evidence_source_ids": (
                    item["counter_evidence_source_ids"]
                ),
                "recommended_control_codes": (
                    [_PO_ALLOWED_CONTROL_CODES[candidate.candidate_type][0]]
                    if item["verdict"] == "RISK"
                    else []
                ),
            }
        )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(replay, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    assert result.supporting_primary_overlap_count >= 4
    assert result.decision_summary_perspective_warning_count >= 1
    assert result.repair_count == 0
    assert result.findings
    for finding in result.findings:
        formal_text = "".join(
            (
                finding.title,
                finding.issue,
                finding.impact_to_our_party,
                finding.suggestion,
            )
        )
        assert "我方作为乙方" not in formal_text
        assert finding.perspective == "PARTY_A"
        assert finding.our_party == "杭州戎一教育科技有限公司"
        assert finding.counterparty == "苏州爱兔格人工智能科技有限公司"
        assert finding.category == "RIGHTS_OBLIGATIONS_IMBALANCE"
        assert finding.risk_type in {
            "UNILATERAL_CONTROL_RISK",
            "CHANGE_CONTROL_RISK",
        }


def test_po_model_never_controls_check_category_or_risk_type() -> None:
    request = _po_request()
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(
                    _po_candidate_payload(
                        request,
                        risk_check_code="PO-004",
                        risk_candidate_type="QUALITY_STANDARD_UNMEASURABLE",
                    ),
                    ensure_ascii=False,
                ),
                "performance_obligations",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            request,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    assert result.ignored_model_check_code_count == 0
    assert result.ignored_model_category_count == 0
    assert result.category_conflict_count == 0
    finding = next(item for item in result.findings if item.check_code == "PO-004")
    assert finding.category == "RIGHTS_OBLIGATIONS_IMBALANCE"
    assert finding.risk_type == "PO-004_RISK"


def test_po_unknown_candidate_is_rejected_without_repair() -> None:
    request = _po_request()
    payload = _po_candidate_payload(request)
    payload["candidate_decisions"][0]["candidate_id"] = (
        "risk-candidate-" + "f" * 32
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(
                runtime_factory=lambda _tenant: runtime
            ).review(
                request,
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code == "RISK_CANDIDATE_UNKNOWN"
    assert len(runtime.calls) == 1


def test_candidate_and_unique_allowed_risk_type_resolution_rules() -> None:
    spec = GenericCheckSpec(
        check_code="PO-004",
        review_question="检查服务质量和验收",
        allowed_categories=["RIGHTS_OBLIGATIONS_IMBALANCE"],
        allowed_risk_types=["SERVICE_LEVEL_RISK", "OTHER_SERVICE_RISK"],
        required_ir_types=["obligations"],
        criticality="REQUIRED",
    )
    first = _candidate("a")
    second_same = _candidate("b")
    second_other = _candidate(
        "c",
        candidate_type="ACCEPTANCE_MECHANISM_ABSENT",
    )
    base = {
        "risk_level": "HIGH",
        "title": "质量标准不可衡量",
        "issue": "质量与验收机制不完整。",
        "impact_to_our_party": "我方难以验收。",
        "suggestion": "补充指标和程序。",
        "evidence_source_ids": ["risk-es-" + "1" * 32],
    }

    with pytest.raises(DirectReviewError) as raised:
        _resolve_finding_fields(
            parent_check_code="PO-004",
            spec=spec,
            value=GenericModelFindingDraft.model_validate(
                {**base, "candidate_ids": [first.candidate_id]}
            ),
            candidates_by_id={first.candidate_id: first},
        )
    assert raised.value.code == "RISK_TYPE_REQUIRED"

    candidate_type_spec = spec.model_copy(
        update={
            "allowed_risk_types": [
                "QUALITY_STANDARD_UNMEASURABLE",
                "OTHER_SERVICE_RISK",
            ]
        }
    )
    resolution = _resolve_finding_fields(
        parent_check_code="PO-004",
        spec=candidate_type_spec,
        value=GenericModelFindingDraft.model_validate(
            {
                **base,
                "candidate_ids": [
                    first.candidate_id,
                    second_same.candidate_id,
                ],
            }
        ),
        candidates_by_id={
            first.candidate_id: first,
            second_same.candidate_id: second_same,
        },
    )
    assert resolution.value.risk_type == "QUALITY_STANDARD_UNMEASURABLE"
    assert resolution.risk_type_source == "CANDIDATE"

    with pytest.raises(DirectReviewError) as raised:
        _resolve_finding_fields(
            parent_check_code="PO-004",
            spec=candidate_type_spec,
            value=GenericModelFindingDraft.model_validate(
                {
                    **base,
                    "candidate_ids": [
                        first.candidate_id,
                        second_other.candidate_id,
                    ],
                }
            ),
            candidates_by_id={
                first.candidate_id: first,
                second_other.candidate_id: second_other,
            },
        )
    assert raised.value.code == "RISK_TYPE_REQUIRED"


def test_candidate_validation_rejects_unknown_cross_check_and_risk_conflict() -> None:
    spec = GenericCheckSpec(
        check_code="PO-004",
        review_question="检查服务质量和验收",
        allowed_categories=["RIGHTS_OBLIGATIONS_IMBALANCE"],
        allowed_risk_types=[
            "QUALITY_STANDARD_UNMEASURABLE",
            "OTHER_SERVICE_RISK",
        ],
        required_ir_types=["obligations"],
        criticality="REQUIRED",
    )
    candidate = _candidate("a")
    cross_check = _candidate("b", check_code="PO-003")
    base = {
        "risk_level": "HIGH",
        "title": "质量标准不可衡量",
        "issue": "质量与验收机制不完整。",
        "impact_to_our_party": "我方难以验收。",
        "suggestion": "补充指标和程序。",
        "evidence_source_ids": ["risk-es-" + "1" * 32],
    }

    for candidate_id, candidates, error_code in (
        (
            "risk-candidate-" + "f" * 32,
            {},
            "RISK_CANDIDATE_UNKNOWN",
        ),
        (
            cross_check.candidate_id,
            {cross_check.candidate_id: cross_check},
            "RISK_CANDIDATE_CHECK_CONFLICT",
        ),
    ):
        with pytest.raises(DirectReviewError) as raised:
            _resolve_finding_fields(
                parent_check_code="PO-004",
                spec=spec,
                value=GenericModelFindingDraft.model_validate(
                    {**base, "candidate_ids": [candidate_id]}
                ),
                candidates_by_id=candidates,
            )
        assert raised.value.code == error_code

    with pytest.raises(DirectReviewError) as raised:
        _resolve_finding_fields(
            parent_check_code="PO-004",
            spec=spec,
            value=GenericModelFindingDraft.model_validate(
                {
                    **base,
                    "candidate_ids": [candidate.candidate_id],
                    "risk_type": "OTHER_SERVICE_RISK",
                }
            ),
            candidates_by_id={candidate.candidate_id: candidate},
        )
    assert raised.value.code == "RISK_TYPE_CONFLICT"


def test_structural_enrichment_is_semantically_safe_but_explicit_change_is_not() -> None:
    payload = _po_payload(
        check_code="PO-004",
        evidence_source_ids=["risk-es-" + "1" * 32],
        include_technical_fields=False,
    )
    before = _generic_repair_snapshot(payload)
    repaired = json.loads(json.dumps(payload, ensure_ascii=False))
    finding = repaired["check_results"][3]["findings"][0]
    finding.update(
        {
            "check_code": "PO-004",
            "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "risk_type": "PO-004_RISK",
        }
    )
    after = _generic_repair_snapshot(repaired)

    _validate_generic_semantic_preservation(
        before,
        after,
        repair_type="SCHEMA_REPAIR",
    )
    changed = json.loads(json.dumps(repaired, ensure_ascii=False))
    changed["check_results"][3]["findings"][0]["risk_type"] = "OTHER_RISK"
    with pytest.raises(DirectReviewError) as raised:
        _validate_generic_semantic_preservation(
            after,
            _generic_repair_snapshot(changed),
            repair_type="SCHEMA_REPAIR",
        )
    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"


def test_evidence_selection_repair_can_change_only_source_ids() -> None:
    before = {
        "PO-003": {
            "protected_check_fields": {
                "status": "REVIEWED",
                "decision_note": "已检查。",
            },
            "findings": [
                {
                    "check_code": "PO-003",
                    "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
                    "risk_type": "PO-003_RISK",
                    "risk_level": "MEDIUM",
                    "title": "配合义务不完整",
                    "issue": "配合义务和延迟边界不完整。",
                    "impact_to_our_party": "我方可能承担延迟。",
                    "suggestion": "补充配合和顺延。",
                    "evidence": [],
                    "evidence_source_ids": ["risk-es-" + "1" * 32],
                }
            ],
        }
    }
    after = json.loads(json.dumps(before, ensure_ascii=False))
    after["PO-003"]["findings"][0]["evidence_source_ids"] = [
        "risk-es-" + "2" * 32
    ]

    _validate_generic_semantic_preservation(
        before,
        after,
        repair_type="EVIDENCE_SELECTION_REPAIR",
    )
    after["PO-003"]["findings"][0]["risk_level"] = "HIGH"
    with pytest.raises(DirectReviewError) as raised:
        _validate_generic_semantic_preservation(
            before,
            after,
            repair_type="EVIDENCE_SELECTION_REPAIR",
        )
    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"


def test_po_domain_gate_rejects_non_po_legal_root() -> None:
    finding = SimpleNamespace(
        check_code="PO-004",
        title="知识产权归属缺失",
        issue="合同没有约定知识产权归属。",
        impact_to_our_party="可能影响成果使用。",
        suggestion="补充知识产权条款。",
    )

    with pytest.raises(DirectReviewError) as raised:
        _validate_domain_safety(finding)

    assert raised.value.code == "RISK_PO_DOMAIN_LEAKAGE"


def test_po_domain_gate_allows_cross_domain_remediation_in_suggestion() -> None:
    finding = SimpleNamespace(
        check_code="PO-002",
        title="甲方检查权缺少程序限制",
        issue="甲方可以随时检查，合同没有约定通知时间和合理范围。",
        impact_to_our_party="我方履约可能受到不合理干扰。",
        suggestion=(
            "限制检查范围、提前通知，并增加保密义务、数据删除、保险、担保、"
            "审计和书面确认等辅助保护措施。"
        ),
    )

    _validate_domain_safety(finding)


def test_po_domain_gate_rejects_cross_domain_core_issue() -> None:
    finding = SimpleNamespace(
        check_code="PO-002",
        title="合同缺少保密条款",
        issue="合同未约定保密义务和保密信息范围。",
        impact_to_our_party="商业秘密可能泄露。",
        suggestion="补充保密条款。",
    )

    with pytest.raises(DirectReviewError) as raised:
        _validate_domain_safety(finding)

    assert raised.value.code == "RISK_PO_DOMAIN_LEAKAGE"


def test_po_acceptance_mode_stops_before_evidence_selection_repair() -> None:
    request = _po_request()
    catalog, _ir_refs, _anchor_refs = _po_catalog(request)
    wrong_source_id = next(
        source_id
        for source_id in catalog.allowed_source_ids_by_check["PO-006"]
        if source_id
        not in set(catalog.allowed_source_ids_by_check["PO-002"])
    )
    payload = _po_candidate_payload(request)
    decision = next(
        item
        for item in payload["candidate_decisions"]
        if item["candidate_id"]
        == next(
            candidate.candidate_id
            for candidate in _build_generic_candidates(
                request,
                _generic_prompt(request)[1],
                {
                    excerpt.anchor_id: ref
                    for ref, excerpt in _generic_prompt(request)[2].items()
                },
            )
            if candidate.check_code == "PO-002"
        )
    )
    decision["counter_evidence_source_ids"] = [wrong_source_id]
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "performance_obligations",
            )
        ]
    )
    reviewer = GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime)

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            reviewer.review(
                request,
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
                allow_evidence_selection_repair=False,
            )
        )

    assert raised.value.code == "RISK_COUNTER_EVIDENCE_NOT_ALLOWED"
    assert len(runtime.calls) == 1


def test_canonical_risk_key_includes_level_and_sorted_anchors() -> None:
    finding = FindingDraft.model_validate(
        {
            "finding_local_id": "finding-" + "1" * 32,
            "source_unit_id": "performance_obligations",
            "domain": "performance_obligations",
            "check_code": "PO-004",
            "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "risk_type": "SERVICE_LEVEL_RISK",
            "risk_level": "MEDIUM",
            "title": "服务标准不明确",
            "issue": "服务标准不可衡量。",
            "impact_to_our_party": "验收和追责困难。",
            "suggestion": "补充可衡量指标。",
            "perspective": "PARTY_A",
            "our_party": "甲方",
            "counterparty": "乙方",
            "evidence_candidates": [
                {
                    "evidence_local_id": "evidence-" + "2" * 32,
                    "finding_local_id": "finding-" + "1" * 32,
                    "evidence_type": "TEXT_QUOTE",
                    "source_ir_item_id": "ir-1",
                    "anchor_id": "anchor-2",
                    "block_id": "block-2",
                    "page_number": None,
                    "char_start": 0,
                    "char_end": 4,
                    "quoted_text": "满足要求",
                    "quoted_text_hash": (
                        "sha256:"
                        + hashlib.sha256("满足要求".encode("utf-8")).hexdigest()
                    ),
                    "checked_scope": None,
                    "verification_note": None,
                }
            ],
        }
    )

    assert _canonical_risk_key(finding) == (
        "PO-004",
        "SERVICE_LEVEL_RISK",
        "MEDIUM",
        ("anchor-2",),
    )


def test_fva002_cannot_assert_unseen_external_facts() -> None:
    body = json.dumps(_payload(fva002_external_assertion=True), ensure_ascii=False)
    runtime = FakeRuntime(
        [
            _completion(body, "formation_validity_authority"),
            _completion(body, "formation_validity_authority", repair_no=1),
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code == "RISK_FVA_EXTERNAL_FACT_ASSERTED"


def test_fva002_external_verification_is_not_a_finding() -> None:
    body = json.dumps(
        _payload(
            fva002_assessment="EXTERNAL_VERIFICATION_REQUIRED",
            fva002_external_required=True,
        ),
        ensure_ascii=False,
    )
    runtime = FakeRuntime(
        [_completion(body, "formation_validity_authority")]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    fva002 = next(
        item for item in result.check_results if item.check_code == "FVA-002"
    )
    assert fva002.status == "REVIEWED"
    assert fva002.reason_code == "INSUFFICIENT_EVIDENCE"
    assert fva002.finding_local_ids == []
    assert result.fva_assessments[0].assessment_type == (
        "EXTERNAL_VERIFICATION_REQUIRED"
    )
    assert not any(
        item.check_code == "FVA-002" for item in result.findings
    )


def test_fva002_external_verification_rejects_personnel_scope_leakage() -> None:
    payload = _payload(
        fva002_assessment="EXTERNAL_VERIFICATION_REQUIRED",
        fva002_external_required=True,
    )
    payload["check_results"][1]["decision_note"] = (
        "需核验授权委托书，并核验乙方上岗人员资质和劳动合同。"
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "formation_validity_authority",
            )
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code == "RISK_FVA002_SCOPE_LEAKAGE"
    assert len(runtime.calls) == 1


def test_fva002_explicit_textual_conflict_requires_source_evidence() -> None:
    payload = _payload(fva002_external_assertion=True)
    fva002 = payload["check_results"][1]
    fva002["decision_note"] = "签署主体与合同首部主体存在明确文本冲突。"
    finding = fva002["findings"][0]
    finding.update(
        {
            "title": "签署主体与合同首部不一致",
            "issue": "合同首部和签署处记载了不同主体。",
            "impact_to_our_party": "可能造成合同权利义务主体指向不明。",
            "suggestion": "统一合同首部与签署处的主体全称。",
            "evidence": [
                {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": "I001",
                    "evidence_ref": "A001",
                    "checked_scope": None,
                    "verification_note": None,
                }
            ],
        }
    )
    body = json.dumps(payload, ensure_ascii=False)
    runtime = FakeRuntime(
        [_completion(body, "formation_validity_authority")]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    fva002_result = next(
        item for item in result.check_results if item.check_code == "FVA-002"
    )
    assert fva002_result.reason_code == "RISK_IDENTIFIED"
    assert len(fva002_result.finding_local_ids) == 1
    assert result.fva_assessments[0].assessment_type == (
        "TEXTUAL_AUTHORITY_RISK"
    )


def test_fva002_explicit_no_authority_text_can_form_a_textual_risk() -> None:
    payload = _payload(fva002_external_assertion=True)
    fva002 = payload["check_results"][1]
    fva002["decision_note"] = "合同原文明示签署人无授权。"
    finding = fva002["findings"][0]
    finding.update(
        {
            "title": "合同原文存在无授权记载",
            "issue": "合同原文明示签署人无授权，属于文本内可见问题。",
            "impact_to_our_party": "签署权限在合同文本内存在明确疑点。",
            "suggestion": "更正文本并取得有效授权后签署。",
            "evidence": [
                {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": "I001",
                    "evidence_ref": "A001",
                    "checked_scope": None,
                    "verification_note": None,
                }
            ],
        }
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(payload, ensure_ascii=False),
                "formation_validity_authority",
            )
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    assert result.fva_assessments[0].assessment_type == (
        "TEXTUAL_AUTHORITY_RISK"
    )
    assert any(item.check_code == "FVA-002" for item in result.findings)


@pytest.mark.parametrize(
    ("assessment", "external_required", "with_finding"),
    [
        ("TEXTUAL_AUTHORITY_RISK", False, False),
        ("EXTERNAL_VERIFICATION_REQUIRED", False, False),
        ("EXTERNAL_VERIFICATION_REQUIRED", True, True),
        ("NO_VISIBLE_ISSUE", True, False),
    ],
)
def test_fva002_rejects_inconsistent_state_combinations(
    assessment,
    external_required,
    with_finding,
) -> None:
    payload = _payload(
        fva002_external_assertion=with_finding,
        fva002_assessment=assessment,
        fva002_external_required=external_required,
    )
    if with_finding:
        finding = payload["check_results"][1]["findings"][0]
        finding.update(
            {
                "title": "合同文本授权条款存在冲突",
                "issue": "合同文本关于签署授权的记载前后不一致。",
                "impact_to_our_party": "可能造成签署权限边界不明。",
                "suggestion": "统一授权条款。",
            }
        )
        finding["evidence"] = [
            {
                "evidence_type": "TEXT_QUOTE",
                "ir_ref": "I001",
                "evidence_ref": "A001",
                "checked_scope": None,
                "verification_note": None,
            }
        ]
    body = json.dumps(payload, ensure_ascii=False)
    runtime = FakeRuntime(
        [
            _completion(body, "formation_validity_authority"),
            _completion(body, "formation_validity_authority", repair_no=1),
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code in {
        "RISK_FVA002_STATE_INCONSISTENT",
        "RISK_REPAIR_SEMANTICS_CHANGED",
    }


def test_fva002_no_visible_issue_cannot_claim_external_authority_was_verified() -> None:
    payload = _payload()
    payload["check_results"][1]["decision_note"] = "已核验授权，未发现问题。"
    body = json.dumps(payload, ensure_ascii=False)
    runtime = FakeRuntime(
        [
            _completion(body, "formation_validity_authority"),
            _completion(body, "formation_validity_authority", repair_no=1),
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code == "RISK_FVA_EXTERNAL_FACT_ASSERTED"


def test_fva002_assessment_fields_are_rejected_on_other_checks() -> None:
    payload = _payload()
    payload["check_results"][0]["assessment_type"] = "NO_VISIBLE_ISSUE"
    payload["check_results"][0]["external_verification_required"] = False
    body = json.dumps(payload, ensure_ascii=False)
    runtime = FakeRuntime(
        [
            _completion(body, "formation_validity_authority"),
            _completion(body, "formation_validity_authority", repair_no=1),
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code == "RISK_DIRECT_SCHEMA_INVALID"


def test_structural_repair_can_only_add_compatible_evidence_type() -> None:
    valid = _payload()
    first = json.loads(json.dumps(valid, ensure_ascii=False))
    del first["check_results"][0]["findings"][0]["evidence"][0]["evidence_type"]
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(first, ensure_ascii=False),
                "formation_validity_authority",
            ),
            _completion(
                json.dumps(valid, ensure_ascii=False),
                "formation_validity_authority",
                repair_no=1,
            ),
        ]
    )

    result = asyncio.run(
        GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
            _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
        )
    )

    assert result.repair_count == 1
    assert result.attempt_diagnostics[1].semantic_preservation_passed is True
    assert result.findings[0].evidence_candidates[0].evidence_type == "TEXT_QUOTE"


def test_structural_repair_cannot_change_fva002_assessment() -> None:
    first = _payload()
    del first["check_results"][0]["findings"][0]["evidence"][0]["evidence_type"]
    repaired = _payload(
        fva002_assessment="EXTERNAL_VERIFICATION_REQUIRED",
        fva002_external_required=True,
    )
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(first, ensure_ascii=False),
                "formation_validity_authority",
            ),
            _completion(
                json.dumps(repaired, ensure_ascii=False),
                "formation_validity_authority",
                repair_no=1,
            ),
        ]
    )

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )

    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"


def test_semantic_change_failure_persists_both_complete_attempts() -> None:
    first = json.dumps(
        _payload(fva002_external_assertion=True),
        ensure_ascii=False,
    )
    repaired = json.dumps(_payload(), ensure_ascii=False)
    runtime = FakeRuntime(
        [
            _completion(first, "formation_validity_authority"),
            _completion(repaired, "formation_validity_authority", repair_no=1),
        ]
    )
    persisted: list[GenericAttemptArtifact] = []

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
                attempt_artifact_sink=persisted.append,
            )
        )

    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"
    assert [item.attempt_type for item in persisted] == ["INITIAL", "REPAIR"]
    assert [item.raw_response for item in persisted] == [first, repaired]
    assert persisted[0].accepted is False
    assert persisted[0].acceptance_reason == "REPAIR_REQUIRED"
    assert persisted[0].validation_errors["domain_safety"]
    assert persisted[1].accepted is False
    assert persisted[1].semantic_preservation_passed is False
    assert persisted[1].validation_errors["semantic_preservation"]
    assert persisted[1].before_summary is not None
    assert persisted[1].after_summary is not None
    assert persisted[1].semantic_diff is not None
    assert persisted[1].semantic_diff["changed_check_codes"] == ["FVA-002"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["check_results"].pop(),
        lambda body: body["check_results"].__setitem__(
            4, {**body["check_results"][4], "check_code": "FVA-004"}
        ),
        lambda body: body["check_results"][0]["findings"][0]["evidence"][0].update(
            {"evidence_ref": "A999"}
        ),
    ],
)
def test_generic_direct_review_never_returns_partial_or_invalid_evidence(mutate) -> None:
    payload = _payload()
    mutate(payload)
    body = json.dumps(payload, ensure_ascii=False)
    runtime = FakeRuntime(
        [
            _completion(body, "formation_validity_authority"),
            _completion(body, "formation_validity_authority", repair_no=1),
        ]
    )

    with pytest.raises(DirectReviewError):
        asyncio.run(
            GenericBaseDirectReviewer(runtime_factory=lambda _tenant: runtime).review(
                _request(),
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
            )
        )


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.cancelled = 0

    async def wait(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


TEST_CONTRACT_HASH = "sha256:" + "1" * 64
TEST_FIXTURE_ID = "service-outsourcing-0829-v1"


def _coverage(codes: list[str]) -> list[CheckCoverageResult]:
    return [
        CheckCoverageResult(
            check_code=code,
            status="REVIEWED",
            reason_code="NO_RISK_IDENTIFIED",
            decision_note="固定无模型Bundle测试。",
            finding_local_ids=[],
        )
        for code in codes
    ]


def _metric(
    unit_id: str,
    suffix: str,
    *,
    prompt_tokens: int | None = 100,
    cached_tokens: int | None = 0,
) -> LlmCallMetric:
    return LlmCallMetric(
        review_unit_id=unit_id,
        repair_no=0,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=20,
        total_tokens=(
            prompt_tokens + 20 if prompt_tokens is not None else None
        ),
        time_to_first_token_ms=10,
        model_duration_ms=30,
        trace_id=f"trace-{suffix}",
        provider_request_id=f"request-{suffix}",
        finish_reason="stop",
    )


def _diagnostic(unit_id: str, suffix: str) -> LlmAttemptDiagnostic:
    return LlmAttemptDiagnostic(
        repair_no=0,
        raw_content='{"check_results":[]}',
        raw_content_sha256=(
            "sha256:"
            + hashlib.sha256(b'{"check_results":[]}').hexdigest()
        ),
        prompt_tokens=100,
        cached_tokens=0,
        completion_tokens=20,
        total_tokens=120,
        time_to_first_token_ms=10,
        model_duration_ms=30,
        trace_id=f"trace-{suffix}",
        provider_request_id=f"request-{suffix}",
        finish_reason="stop",
    )


def test_bundle_summary_uses_formal_absence_fields_as_stable_identity() -> None:
    evidence = EvidenceCandidate(
        evidence_local_id="evidence-" + "1" * 32,
        finding_local_id="finding-" + "2" * 32,
        evidence_type="ABSENCE",
        checked_scope="已检查合同全文，未发现履约保障。",
        verification_note="合同没有保函、保证金或退款机制。",
    )

    first = stage63_runner._summary_evidence_source_id(
        evidence,
        source_by_binding={},
        absence_by_scope={},
    )
    second = stage63_runner._summary_evidence_source_id(
        evidence,
        source_by_binding={},
        absence_by_scope={
            ("另一个内部检查范围", "另一个内部验证方法"): "risk-as-stale"
        },
    )

    assert first == second
    assert first.startswith("ABSENCE:")


def test_commercial_bundle_stability_ignores_only_non_core_findings() -> None:
    first = {
        "check_statuses": {
            "CF-003": "REVIEWED",
            "CF-004": "REVIEWED",
        },
        "reason_codes": {
            "CF-003": "NO_RISK_IDENTIFIED",
            "CF-004": "NO_RISK_IDENTIFIED",
        },
        "canonical_source_risk_keys": [
            [
                "CF-005",
                "ADVANCE_PAYMENT_SECURITY_RISK",
                "HIGH",
                [
                    "risk-es-payment",
                    "ABSENCE:" + "1" * 64,
                ],
            ],
            ["CF-007", "DELIVERY_RISK", "MEDIUM", ["risk-es-delivery"]],
        ],
    }
    second = {
        **first,
        "canonical_source_risk_keys": [
            [
                "CF-005",
                "ADVANCE_PAYMENT_SECURITY_RISK",
                "HIGH",
                [
                    "risk-es-payment",
                    "ABSENCE:" + "2" * 64,
                ],
            ]
        ],
    }

    assert not stage63_runner._validate_commercial_bundle_runs(
        [first, second]
    )

    changed = {
        **second,
        "canonical_source_risk_keys": [
            [
                "CF-005",
                "ADVANCE_PAYMENT_SECURITY_RISK",
                "MEDIUM",
                [
                    "risk-es-payment",
                    "ABSENCE:" + "3" * 64,
                ],
            ]
        ],
    }
    assert stage63_runner._validate_commercial_bundle_runs(
        [first, changed]
    )


class FakeGenericReviewer:
    def __init__(
        self,
        tracker: ConcurrencyTracker,
        *,
        fail_unit: str | None = None,
        prompt_tokens_by_unit: dict[str, int | None] | None = None,
    ) -> None:
        self.tracker = tracker
        self.fail_unit = fail_unit
        self.prompt_tokens_by_unit = prompt_tokens_by_unit or {}

    async def review(self, request, **_kwargs) -> ReviewBatchResult:
        await self.tracker.wait()
        if request.unit_id == self.fail_unit:
            raise DirectReviewError("RISK_FAKE_BATCH_FAILED", "injected failure")
        suffix = request.batch_id[-6:]
        prompt_tokens = self.prompt_tokens_by_unit.get(request.unit_id, 100)
        cached_tokens = (
            min(prompt_tokens, 64) if prompt_tokens is not None else None
        )
        metric = _metric(
            request.unit_id,
            suffix,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
        )
        return ReviewBatchResult(
            unit_id=request.unit_id,
            domain=request.unit_id,
            batch_id=request.batch_id,
            status="COMPLETED",
            check_results=_coverage(
                [item.check_code for item in request.assigned_check_specs]
            ),
            findings=[],
            model_call_count=1,
            repair_count=0,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=20,
            total_tokens=(
                prompt_tokens + 20 if prompt_tokens is not None else None
            ),
            duration_ms=30,
            trace_ids=[metric.trace_id],
            call_metrics=[metric],
            attempt_diagnostics=[
                _diagnostic(request.unit_id, suffix)
            ],
            reason_code_enrichment_count=len(request.assigned_check_specs),
            reason_code_rule_version="1.0",
            ignored_model_reason_code_count=0,
        )


class FakeCommercialReviewer:
    def __init__(
        self,
        tracker: ConcurrencyTracker,
        *,
        prompt_tokens: int | None = 100,
    ) -> None:
        self.tracker = tracker
        self.prompt_tokens = prompt_tokens

    async def review(self, request, **_kwargs) -> ReviewUnitResult:
        await self.tracker.wait()
        cached_tokens = (
            min(self.prompt_tokens, 64)
            if self.prompt_tokens is not None
            else None
        )
        metric = _metric(
            "commercial_financial",
            "commercial",
            prompt_tokens=self.prompt_tokens,
            cached_tokens=cached_tokens,
        )
        return ReviewUnitResult(
            unit_id="commercial_financial",
            domain="commercial_financial",
            status="COMPLETED",
            check_results=_coverage(
                [item.check_code for item in request.assigned_check_specs]
            ),
            findings=[],
            model_call_count=1,
            repair_count=0,
            prompt_tokens=self.prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=20,
            total_tokens=(
                self.prompt_tokens + 20
                if self.prompt_tokens is not None
                else None
            ),
            duration_ms=30,
            trace_ids=[metric.trace_id],
            call_metrics=[metric],
            attempt_diagnostics=[
                _diagnostic("commercial_financial", "commercial")
            ],
            reason_code_enrichment_count=8,
            reason_code_rule_version="1.0",
            ignored_model_reason_code_count=0,
        )


class FailingCommercialReviewer(FakeCommercialReviewer):
    async def review(self, request, **_kwargs) -> ReviewUnitResult:
        await self.tracker.wait()
        raise DirectReviewError(
            "RISK_FAKE_COMMERCIAL_FAILED",
            "injected commercial failure",
        )


class BrokenCoverageGenericReviewer(FakeGenericReviewer):
    async def review(self, request, **kwargs) -> ReviewBatchResult:
        result = await super().review(request, **kwargs)
        if request.unit_id == "ip_confidentiality_data":
            return result.model_copy(update={"check_results": []})
        return result


def _run_fake_bundle(plan, *, generic=None, commercial=None, **kwargs):
    tracker = ConcurrencyTracker()
    return asyncio.run(
        execute_base_risk_review_bundle(
            plan,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            contract_hash=TEST_CONTRACT_HASH,
            fixture_id=TEST_FIXTURE_ID,
            generic_reviewer=generic or FakeGenericReviewer(tracker),
            commercial_reviewer=commercial or FakeCommercialReviewer(tracker),
            **kwargs,
        )
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_fixed_fixture_builds_seven_batches_and_parallel_complete_bundle() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()
    attempts: list[GenericAttemptArtifact] = []

    bundle = asyncio.run(
        execute_base_risk_review_bundle(
            plan,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            contract_hash=TEST_CONTRACT_HASH,
            fixture_id=TEST_FIXTURE_ID,
            generic_reviewer=FakeGenericReviewer(tracker),
            commercial_reviewer=FakeCommercialReviewer(tracker),
            attempt_artifact_sink=attempts.append,
        )
    )

    assert len(bundle.batch_results) == 7
    assert [item.unit_id for item in bundle.units] == list(BASE_UNIT_IDS)
    assert [
        item.check_code for unit in bundle.units for item in unit.check_results
    ] == list(EXPECTED_BASE_CHECK_CODES)
    assert bundle.metrics.peak_concurrency == 7
    assert tracker.peak == 7
    assert bundle.metrics.model_call_count == 7
    assert bundle.metrics.tool_call_count == 0
    assert len(attempts) == 1
    assert attempts[0].unit_id == "commercial_financial"
    assert attempts[0].accepted
    assert bundle.identity.contract_hash == TEST_CONTRACT_HASH
    assert bundle.identity.fixture_id == TEST_FIXTURE_ID
    assert bundle.identity.schema_version == "1.0"
    assert len(bundle.metrics.batch_metrics) == 7
    assert len(bundle.metrics.unit_metrics) == 5
    assert bundle.metrics.prompt_budget_policy_version == "2.0"
    assert bundle.metrics.prompt_budget_warning_count == 0
    assert bundle.metrics.prompt_budget_hard_failure_count == 0
    assert bundle.metrics.max_provider_prompt_tokens == 100
    assert bundle.metrics.batches_over_target == []
    assert bundle.metrics.batches_over_hard_limit == []
    assert all(
        item.prompt_budget is not None
        for item in bundle.metrics.batch_metrics
    )
    assert all(
        item.start_offset_ms < bundle.metrics.wall_duration_ms
        for item in bundle.metrics.batch_metrics
    )
    assert bundle_duration_summary([bundle]) == {
        "min": bundle.metrics.wall_duration_ms,
        "median": bundle.metrics.wall_duration_ms,
        "max": bundle.metrics.wall_duration_ms,
    }
    acceptance_summary = stage63_runner._bundle_summary(bundle, plan=plan)
    acceptance_failures = stage63_runner._validate_bundles(
        [acceptance_summary],
        plan=plan,
    )
    assert acceptance_failures
    assert any(
        "Commercial CF-005 Finding is missing" in item
        for item in acceptance_failures
    )

    by_unit = {item.unit_id: item for item in bundle.units}
    assert len(by_unit["formation_validity_authority"].batch_ids) == 1
    assert len(by_unit["commercial_financial"].batch_ids) == 1
    assert len(by_unit["performance_obligations"].batch_ids) == 2
    assert len(by_unit["ip_confidentiality_data"].batch_ids) == 1
    assert len(by_unit["liability_remedies_exit"].batch_ids) == 2

    po_unit = next(
        item
        for item in plan.review_units
        if item.unit_id == "performance_obligations"
    )
    contexts = {item.batch_id: item for item in plan.contexts}
    assert [
        [spec.check_code for spec in contexts[batch_id].check_specs]
        for batch_id in po_unit.batch_ids
    ] == [
        ["PO-001", "PO-003", "PO-004", "PO-007"],
        ["PO-002", "PO-005", "PO-006"],
    ]
    assert all(
        len(contexts[batch_id].definitions)
        + len(contexts[batch_id].projected_ir_items)
        < 101
        for batch_id in po_unit.batch_ids
    )
    for batch_id in po_unit.batch_ids:
        request = generic_request_from_context(contexts[batch_id])
        prompt = _generic_prompt(request)[0]
        assert (
            estimate_tokens_in_text(_PO_CANDIDATE_SYSTEM_PROMPT)
            + estimate_tokens_in_text(prompt)
            > 0
        )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_provider_prompt_soft_warning_does_not_fail_base_bundle() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()
    bundle = _run_fake_bundle(
        plan,
        generic=FakeGenericReviewer(
            tracker,
            prompt_tokens_by_unit={"formation_validity_authority": 6145},
        ),
        commercial=FakeCommercialReviewer(tracker),
    )

    fva_metric = next(
        item
        for item in bundle.metrics.batch_metrics
        if item.unit_id == "formation_validity_authority"
    )
    assert fva_metric.prompt_budget is not None
    assert fva_metric.prompt_budget.budget_status == "SOFT_WARNING"
    assert fva_metric.prompt_budget.tokens_over_target == 145
    assert bundle.metrics.prompt_budget_warning_count == 1
    assert bundle.metrics.prompt_budget_hard_failure_count == 0
    assert bundle.metrics.max_provider_prompt_tokens == 6145
    assert bundle.metrics.batches_over_target == [fva_metric.batch_id]
    assert bundle.metrics.batches_over_hard_limit == []
    assert bundle.status == "COMPLETED"


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_provider_prompt_hard_limit_is_scoped_to_affected_batch() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()

    bundle = _run_fake_bundle(
        plan,
        generic=FakeGenericReviewer(
            tracker,
            prompt_tokens_by_unit={"formation_validity_authority": 7001},
        ),
        commercial=FakeCommercialReviewer(tracker),
    )

    assert bundle.status == "PARTIAL_FAILED"
    failed = [
        item for item in bundle.batch_results if item.status == "FAILED"
    ]
    assert len(failed) == 1
    assert failed[0].unit_id == "formation_validity_authority"
    assert all(item.status == "FAILED" for item in failed[0].check_results)
    assert bundle.metrics.failed_batch_ids == [failed[0].batch_id]
    assert bundle.metrics.prompt_budget_hard_failure_count == 1


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_missing_provider_usage_is_diagnostic_and_not_replaced_by_local_estimate() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()
    bundle = _run_fake_bundle(
        plan,
        generic=FakeGenericReviewer(
            tracker,
            prompt_tokens_by_unit={"formation_validity_authority": None},
        ),
        commercial=FakeCommercialReviewer(tracker),
    )

    fva_metric = next(
        item
        for item in bundle.metrics.batch_metrics
        if item.unit_id == "formation_validity_authority"
    )
    assert fva_metric.prompt_budget is not None
    assert (
        fva_metric.prompt_budget.budget_status
        == "PROVIDER_USAGE_UNAVAILABLE"
    )
    assert fva_metric.prompt_budget.provider_prompt_tokens is None
    assert fva_metric.prompt_budget.tokens_over_target is None
    assert bundle.metrics.prompt_budget_hard_failure_count == 0


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_complete_base_plan_is_stable_for_one_hundred_builds() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plans = [RiskReviewPlanBuilder().build(value) for _ in range(100)]

    assert len({item.plan_id for item in plans}) == 1
    assert len({item.plan_hash for item in plans}) == 1
    assert len(
        {
            json.dumps(
                [
                    context.model_dump(mode="json")
                    for context in item.contexts
                    if str(context.unit_id) in BASE_UNIT_IDS
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in plans
        }
    ) == 1
    plan = plans[0]
    contexts = [
        item for item in plan.contexts if str(item.unit_id) in BASE_UNIT_IDS
    ]
    assert len(contexts) == 7
    assert sum(
        len(item.check_specs) for item in contexts
    ) == len(EXPECTED_BASE_CHECK_CODES)
    assert all(item.estimated_input_tokens < 6000 for item in contexts)
    assert all(
        len(item.definitions) + len(item.projected_ir_items) < 101
        for item in contexts
    )
    assert all(
        len(
            {
                source.source_id
                for source in (
                    *item.evidence_sources,
                    *item.absence_evidence_sources,
                )
            }
        )
        == len(item.evidence_sources) + len(item.absence_evidence_sources)
        for item in contexts
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_fixed_fixture_i039_is_bound_to_a025_and_a044_cannot_be_combined() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    context = next(
        item
        for item in plan.contexts
        if item.unit_id == "performance_obligations"
        and any(spec.check_code == "PO-003" for spec in item.check_specs)
    )
    request = generic_request_from_context(context)
    catalog, ir_refs, anchor_refs = _po_catalog(request)

    assert anchor_refs["A025"].anchor_id in {
        item.anchor_id for item in ir_refs["I039"].source_anchors
    }
    assert anchor_refs["A044"].anchor_id not in {
        item.anchor_id for item in ir_refs["I039"].source_anchors
    }
    a044_item = next(
        (ref, item)
        for ref, item in ir_refs.items()
        if anchor_refs["A044"].anchor_id
        in {anchor.anchor_id for anchor in item.source_anchors}
    )
    assert a044_item[0] == "I054"
    assert a044_item[1].ir_type == "rights"
    po003_allowed = set(catalog.allowed_source_ids_by_check["PO-003"])
    assert not any(
        source.anchor_id == anchor_refs["A044"].anchor_id
        for source_id, source in catalog.evidence_sources.items()
        if source_id in po003_allowed
    )

    legacy = GenericModelFindingDraft.model_validate(
        {
            "check_code": "PO-003",
            "category": "RIGHTS_OBLIGATIONS_IMBALANCE",
            "risk_type": "COOPERATION_DEPENDENCY_RISK",
            "risk_level": "MEDIUM",
            "title": "甲方配合义务缺失，乙方承担延迟风险",
            "issue": "配合义务和延迟归责边界不完整。",
            "impact_to_our_party": "履约延迟责任可能失衡。",
            "suggestion": "明确配合义务、时限和顺延。",
            "evidence": [
                {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": "I039",
                    "evidence_ref": "A044",
                }
            ],
        }
    )
    selected, normalized, ignored = _resolve_po_evidence_source_ids(
        legacy,
        catalog,
        ir_refs,
        anchor_refs,
    )
    assert len(selected) == 1
    assert catalog.evidence_sources[selected[0]].anchor_id == anchor_refs[
        "A025"
    ].anchor_id
    assert normalized == 1
    assert ignored == 2


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_one_batch_failure_preserves_other_validated_results() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()

    bundle = asyncio.run(
        execute_base_risk_review_bundle(
            plan,
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            contract_hash=TEST_CONTRACT_HASH,
            fixture_id=TEST_FIXTURE_ID,
            generic_reviewer=FakeGenericReviewer(
                tracker,
                fail_unit="formation_validity_authority",
            ),
            commercial_reviewer=FakeCommercialReviewer(tracker),
        )
    )

    assert bundle.status == "PARTIAL_FAILED"
    assert len(bundle.units) == 5
    assert len(bundle.metrics.failed_batch_ids) == 1
    failed_unit = next(
        item
        for item in bundle.units
        if item.unit_id == "formation_validity_authority"
    )
    assert failed_unit.status == "FAILED"
    assert all(item.status == "FAILED" for item in failed_unit.check_results)
    assert any(
        item.findings
        for item in bundle.units
        if item.unit_id != "formation_validity_authority"
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda plan: plan.model_copy(
                update={
                    "contexts": [
                        plan.contexts[0].model_copy(
                            update={"perspective": "PARTY_B"}
                        ),
                        *plan.contexts[1:],
                    ]
                }
            ),
            "RISK_BASE_IDENTITY_MISMATCH",
        ),
            (
                lambda plan: plan.model_copy(
                    update={
                        "review_units": [
                            item
                            for item in plan.review_units
                            if str(item.unit_id) != "liability_remedies_exit"
                        ]
                    }
                ),
            "RISK_BASE_UNIT_COVERAGE_INVALID",
        ),
        (
            lambda plan: plan.model_copy(
                update={
                    "contexts": [
                        plan.contexts[0].model_copy(
                            update={
                                "check_specs": [
                                    *plan.contexts[0].check_specs,
                                    plan.contexts[0].check_specs[0],
                                ]
                            }
                        ),
                        *plan.contexts[1:],
                    ]
                }
            ),
            "RISK_BASE_CHECK_OWNERSHIP_INVALID",
        ),
        (
            lambda plan: plan.model_copy(
                update={
                    "contexts": [
                        plan.contexts[0].model_copy(
                            update={
                                "evidence_sources": [
                                    plan.contexts[0].evidence_sources[0].model_copy(
                                        update={"generation_id": "generation-stale"}
                                    ),
                                    *plan.contexts[0].evidence_sources[1:],
                                ]
                            }
                        ),
                        *plan.contexts[1:],
                    ]
                }
            ),
            "RISK_BASE_EVIDENCE_GENERATION_MISMATCH",
        ),
    ],
)
def test_bundle_rejects_inconsistent_or_incomplete_plan_inputs(
    mutate,
    expected_code: str,
) -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = mutate(RiskReviewPlanBuilder().build(value))

    with pytest.raises(BaseBundleExecutionError) as raised:
        _run_fake_bundle(plan)

    assert raised.value.code == expected_code
    assert raised.value.failure.completed_batch_count == 0
    assert not raised.value.failure.diagnostic_batch_results


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_bundle_timeout_is_atomic_and_returns_no_formal_partial_result() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)

    with pytest.raises(BaseBundleExecutionError) as raised:
        _run_fake_bundle(plan, batch_timeout_seconds=0.001)

    assert raised.value.code == "RISK_BASE_BATCH_TIMEOUT"
    assert raised.value.failure.status == "FAILED"
    assert not hasattr(raised.value.failure, "findings")


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_bundle_cancel_is_atomic_and_does_not_start_formal_result() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(BaseBundleExecutionError) as raised:
        _run_fake_bundle(plan, cancel_event=cancel_event)

    assert raised.value.code == "RISK_BASE_BUNDLE_CANCELLED"
    assert raised.value.failure.status == "FAILED"
    assert raised.value.failure.completed_batch_count == 0
    assert not hasattr(raised.value.failure, "findings")


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_commercial_batch_failure_preserves_other_units() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()

    bundle = _run_fake_bundle(
        plan,
        generic=FakeGenericReviewer(tracker),
        commercial=FailingCommercialReviewer(tracker),
    )

    assert bundle.status == "PARTIAL_FAILED"
    commercial = next(
        item for item in bundle.units if item.unit_id == "commercial_financial"
    )
    assert commercial.status == "FAILED"
    assert all(item.status == "FAILED" for item in commercial.check_results)
    assert any(
        item.findings
        for item in bundle.units
        if item.unit_id != "commercial_financial"
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_unit_merge_failure_fails_the_whole_bundle_without_partial_findings() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    tracker = ConcurrencyTracker()

    with pytest.raises(BaseBundleExecutionError) as raised:
        _run_fake_bundle(
            plan,
            generic=BrokenCoverageGenericReviewer(tracker),
            commercial=FakeCommercialReviewer(tracker),
        )

    assert raised.value.failure.status == "FAILED"
    assert raised.value.failure.failed_unit_id == "ip_confidentiality_data"
    assert raised.value.failure.completed_batch_count == 7
    assert not hasattr(raised.value.failure, "findings")


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_fixed_fixture_projects_all_four_new_units_without_cross_unit_checks() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    contexts = [
        item for item in plan.contexts if item.unit_id != "commercial_financial"
    ]

    assert len(contexts) == 6
    for context in contexts:
        request = generic_request_from_context(context)
        assert request.unit_id == context.unit_id
        assert request.estimated_input_tokens <= 6000
        assert request.source_excerpts
        assert {
            item.check_code for item in request.assigned_check_specs
        }.issubset(set(EXPECTED_BASE_CHECK_CODES))
        assert all(
            item.check_code.startswith(
                {
                    "formation_validity_authority": "FVA-",
                    "performance_obligations": "PO-",
                    "ip_confidentiality_data": "ICD-",
                    "liability_remedies_exit": "LRE-",
                }[request.unit_id]
            )
            for item in request.assigned_check_specs
        )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_lre_fixture_plan_has_two_bounded_batches_and_all_eight_checks() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plans = [RiskReviewPlanBuilder().build(value) for _ in range(100)]
    assert len({item.plan_id for item in plans}) == 1
    assert len({item.plan_hash for item in plans}) == 1
    plan = plans[0]
    unit = next(
        item
        for item in plan.review_units
        if item.unit_id == "liability_remedies_exit"
    )
    assert len(unit.batch_ids) == 2
    contexts = {
        item.batch_id: generic_request_from_context(item)
        for item in plan.contexts
        if item.batch_id in unit.batch_ids
    }
    assert {
        item.check_code
        for context in contexts.values()
        for item in context.assigned_check_specs
    } == {f"LRE-{index:03d}" for index in range(1, 9)}
    assert all(
        len(context.definitions) + len(context.projected_ir_items) < 101
        for context in contexts.values()
    )
    assert all(
        context.estimated_input_tokens < 6000
        and (
            estimate_tokens_in_text(_PO_CANDIDATE_SYSTEM_PROMPT)
            + estimate_tokens_in_text(_generic_prompt(context)[0])
        )
        > 0
        for context in contexts.values()
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_lre_fixture_candidate_oracle_and_absence_sources_are_deterministic() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    contexts = [
        generic_request_from_context(item)
        for item in plan.contexts
        if item.unit_id == "liability_remedies_exit"
    ]
    candidates = []
    absence_by_check = {}
    for context in contexts:
        prompt, ir_refs, anchor_refs = _generic_prompt(context)
        assert (
            "allowed_supporting_evidence_source_ids为空，"
            "supporting_evidence_source_ids必须严格返回空数组"
        ) in prompt
        assert "即使两个Candidate将由Python归入同一Canonical Root" in prompt
        current = _build_generic_candidates(
            context,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        _po_evidence_catalog(context, current, ir_refs, anchor_refs)
        candidates.extend(current)
        absence_by_check.update(
            {item.check_code: item for item in context.absence_evidence_sources}
        )
    by_type = {item.candidate_type: item for item in candidates}
    assert set(by_type) == {
        "BROAD_BREACH_TRIGGER_REVIEW",
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "CUMULATIVE_REMEDIES_REVIEW",
        "LIABILITY_CAP_ABSENT",
        "OVERBROAD_INDEMNITY_REVIEW",
        "TERMINATION_RIGHTS_REVIEW",
        "FORCE_MAJEURE_MECHANISM_ABSENT",
        "DISPUTE_RESOLUTION_ABSENT",
    }
    assert set(absence_by_check) == {"LRE-003", "LRE-007", "LRE-008"}
    assert not {
        "AUTOMATIC_RENEWAL",
        "RESTRICTED_EXIT_WINDOW",
        "DISPUTE_CLAUSE_CONFLICT",
        "FOREIGN_OR_BURDENSOME_FORUM",
    } & set(by_type)
    termination = by_type["TERMINATION_RIGHTS_REVIEW"]
    all_ir = {
        ref: item
        for context in contexts
        for _, ir_refs, _ in [_generic_prompt(context)]
        for ref, item in ir_refs.items()
    }
    assert {
        all_ir[ref].ir_type for ref in termination.candidate_ir_refs
    } == {"termination_terms"}
    assert len(termination.primary_evidence_source_ids) <= 20
    assert not set(termination.primary_evidence_source_ids) & set(
        termination.allowed_counter_evidence_source_ids
    )
    assert not set(termination.primary_evidence_source_ids) & set(
        termination.allowed_supporting_evidence_source_ids
    )
    assert set(termination.primary_evidence_source_ids).issubset(
        _candidate_allowed_source_ids(termination, "Counter")
    )
    assert by_type["LIABILITY_CAP_ABSENT"].context_primary_evidence_source_ids
    broad_breach = by_type["BROAD_BREACH_TRIGGER_REVIEW"]
    assert broad_breach.canonical_root_type == "UNBOUNDED_LIABILITY_EXPOSURE"
    assert broad_breach.primary_evidence_source_ids == [
        "risk-es-40e3d874d33a7b864fe154f416b442ea"
    ]
    for candidate_type in (
        "FORCE_MAJEURE_MECHANISM_ABSENT",
        "DISPUTE_RESOLUTION_ABSENT",
    ):
        candidate = by_type[candidate_type]
        assert candidate.candidate_ir_refs == []
        assert set(candidate.deterministic_severity_factors) == {
            "MISSING_CORE_MECHANISM",
            "NO_EFFECTIVE_REMEDY",
        }


def test_lre_broad_breach_precondition_requires_self_contained_trigger() -> None:
    assert not lre_has_broad_breach_trigger("否则构成重大违约")
    assert not lre_has_broad_breach_trigger("否则亦为重大违约")
    assert lre_has_broad_breach_trigger(
        "乙方违反本合同任何约定均构成重大违约"
    )
    assert lre_has_broad_breach_trigger(
        "甲方可自行认定乙方构成重大违约"
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_lre_fixture_severity_factors_are_closed_and_evidence_gated() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    for context_value in plan.contexts:
        if context_value.unit_id != "liability_remedies_exit":
            continue
        request = generic_request_from_context(context_value)
        _, ir_refs, anchor_refs = _generic_prompt(request)
        candidates = _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        catalog = _po_evidence_catalog(
            request,
            candidates,
            ir_refs,
            anchor_refs,
        )
        for candidate in candidates:
            assert set(candidate.allowed_severity_factors).issubset(
                set(_LRE_SEVERITY_FACTOR_POLICIES)
            )
            assert candidate.check_code in {
                check_code
                for factor_code in candidate.allowed_severity_factors
                for check_code in _LRE_SEVERITY_FACTOR_POLICIES[
                    factor_code
                ].allowed_check_codes
            }
            text_sources = [
                catalog.evidence_sources[source_id]
                for source_id in candidate.primary_evidence_source_ids
                if source_id in catalog.evidence_sources
            ]
            absence_sources = [
                catalog.absence_sources[source_id]
                for source_id in candidate.primary_evidence_source_ids
                if source_id in catalog.absence_sources
            ]
            for factor_code in candidate.deterministic_severity_factors:
                if factor_code == "MISSING_CORE_MECHANISM":
                    assert absence_sources
                    continue
                accepted, _ = _lre_factor_has_required_evidence(
                    factor_code,
                    text_sources=text_sources,
                    absence_sources=absence_sources,
                )
                assert accepted, (candidate.candidate_type, factor_code)
    assert {
        "ADD_AGGREGATE_LIABILITY_CAP",
        "ADD_CURE_PERIOD",
        "ADD_TERMINATION_SETTLEMENT",
        "CLARIFY_FORCE_MAJEURE",
        "CHOOSE_SINGLE_DISPUTE_FORUM",
    }.issubset(
        {
            code
            for codes in _LRE_ALLOWED_CONTROL_CODES.values()
            for code in codes
        }
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_lre_cumulative_remedies_financial_impact_does_not_double_escalate() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    candidates: list[DeterministicRiskCandidate] = []
    for context_value in plan.contexts:
        if context_value.unit_id != "liability_remedies_exit":
            continue
        request = generic_request_from_context(context_value)
        _, ir_refs, anchor_refs = _generic_prompt(request)
        candidates.extend(
            _build_generic_candidates(
                request,
                ir_refs,
                {item.anchor_id: ref for ref, item in anchor_refs.items()},
            )
        )
    cumulative = next(
        item
        for item in candidates
        if item.candidate_type == "CUMULATIVE_REMEDIES_REVIEW"
    )
    assert (
        _candidate_risk_level(
            cumulative,
            _po_severity_factors(
                ["CUMULATIVE_REMEDIES", "FINANCIAL_IMPACT"]
            ),
        )
        == "MEDIUM"
    )


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_lre_same_batch_root_may_materialize_evidence_from_multiple_checks() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    plan = RiskReviewPlanBuilder().build(value)
    for context_value in plan.contexts:
        if context_value.unit_id != "liability_remedies_exit":
            continue
        request = generic_request_from_context(context_value)
        _, ir_refs, anchor_refs = _generic_prompt(request)
        candidates = _build_generic_candidates(
            request,
            ir_refs,
            {item.anchor_id: ref for ref, item in anchor_refs.items()},
        )
        candidate_types = {item.candidate_type for item in candidates}
        if not {
            "OVERBROAD_LOSS_SCOPE_REVIEW",
            "LIABILITY_CAP_ABSENT",
        } <= candidate_types:
            continue
        catalog = _po_evidence_catalog(
            request,
            candidates,
            ir_refs,
            anchor_refs,
        )
        response = CandidateDecisionResponseRaw(
            candidate_decisions=[
                {
                    "candidate_id": candidate.candidate_id,
                    "verdict": (
                        "NO_RISK"
                        if candidate.candidate_type == "TERMINATION_RIGHTS_REVIEW"
                        else "RISK"
                    ),
                    "decision_summary": "依据当前Candidate的确定性证据完成裁决。",
                    "severity_factors": [],
                    "supporting_evidence_source_ids": [],
                    "counter_evidence_source_ids": (
                        candidate.primary_evidence_source_ids
                        if candidate.candidate_type == "TERMINATION_RIGHTS_REVIEW"
                        else []
                    ),
                    "recommended_control_codes": (
                        []
                        if candidate.candidate_type == "TERMINATION_RIGHTS_REVIEW"
                        else [_candidate_allowed_control_codes(candidate)[0]]
                    ),
                }
                for candidate in candidates
            ]
        )
        _, findings, _, roots, _, _, _ = _materialize_po_candidate_decisions(
            request,
            response,
            candidates,
            catalog,
            ir_refs,
            anchor_refs,
        )
        merged = next(
            item
            for item in roots
            if item.root_type == "UNBOUNDED_LIABILITY_EXPOSURE"
        )
        source_checks = {
            candidate.check_code
            for candidate in candidates
            if candidate.candidate_id in merged.source_candidate_ids
        }
        assert len(source_checks) > 1
        assert any(
            item.finding_local_id == merged.finding_local_id for item in findings
        )
        return
    pytest.fail("Fixture did not produce the expected cross-Check LRE root")


def _lre_merge_evidence(
    finding_id: str,
    suffix: str,
    text: str,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_local_id=f"evidence-{suffix * 32}",
        finding_local_id=finding_id,
        evidence_type="TEXT_QUOTE",
        source_ir_item_id=f"ir-{suffix}",
        anchor_id=f"anchor-{suffix}",
        block_id=f"block-{suffix}",
        char_start=0,
        char_end=len(text),
        quoted_text=text,
        quoted_text_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def test_lre_cross_batch_same_root_is_merged_but_independent_roots_are_not() -> None:
    finding_a_id = "finding-" + "a" * 32
    finding_b_id = "finding-" + "b" * 32
    finding_c_id = "finding-" + "c" * 32

    def finding(
        finding_id: str,
        check_code: str,
        risk_type: str,
        suffix: str,
    ) -> FindingDraft:
        return FindingDraft(
            finding_local_id=finding_id,
            source_unit_id="liability_remedies_exit",
            domain="liability_remedies_exit",
            check_code=check_code,
            category="LIABILITY",
            risk_type=risk_type,
            risk_level="HIGH",
            title="责任风险",
            issue="责任安排存在风险。",
            impact_to_our_party="我方可能承担不合理责任。",
            suggestion="完善责任范围和责任上限。",
            perspective="PARTY_A",
            our_party="甲方",
            counterparty="乙方",
            evidence_candidates=[
                _lre_merge_evidence(finding_id, suffix, f"证据{suffix}")
            ],
        )

    def root(
        root_id: str,
        finding_id: str,
        check_code: str,
        root_type: str,
        candidate_suffix: str,
        source_suffix: str,
    ) -> CanonicalRiskRoot:
        source_id = "risk-es-" + source_suffix * 32
        return CanonicalRiskRoot(
            root_id=root_id,
            domain="liability_remedies_exit",
            check_code=check_code,
            risk_type=root_type,
            root_type=root_type,
            source_candidate_ids=["risk-candidate-" + candidate_suffix * 32],
            primary_evidence_source_ids=[source_id],
            core_primary_evidence_source_ids=[source_id],
            severity_factors=_po_severity_factors(
                ["UNLIMITED_LIABILITY", "INDIRECT_LOSS_EXPOSURE"]
            ),
            recommended_control_codes=["ADD_AGGREGATE_LIABILITY_CAP"],
            root_severity_rule_id=(
                "LRE_UNBOUNDED_LIABILITY_ROOT_V1"
                if root_type == "UNBOUNDED_LIABILITY_EXPOSURE"
                else "LRE_CUMULATIVE_REMEDIES_V1"
            ),
            risk_level="HIGH",
            finding_local_id=finding_id,
        )

    roots = [
        root(
            "risk-root-" + "1" * 32,
            finding_a_id,
            "LRE-004",
            "UNBOUNDED_LIABILITY_EXPOSURE",
            "1",
            "1",
        ),
        root(
            "risk-root-" + "2" * 32,
            finding_b_id,
            "LRE-003",
            "UNBOUNDED_LIABILITY_EXPOSURE",
            "2",
            "2",
        ),
        root(
            "risk-root-" + "3" * 32,
            finding_c_id,
            "LRE-002",
            "CUMULATIVE_REMEDIES_REVIEW",
            "3",
            "3",
        ),
    ]
    merged_findings, merged_roots, replacements = (
        _merge_lre_cross_batch_roots(
            [
                finding(
                    finding_a_id,
                    "LRE-004",
                    "UNBOUNDED_LIABILITY_EXPOSURE",
                    "a",
                ),
                finding(
                    finding_b_id,
                    "LRE-003",
                    "UNBOUNDED_LIABILITY_EXPOSURE",
                    "b",
                ),
                finding(
                    finding_c_id,
                    "LRE-002",
                    "CUMULATIVE_REMEDIES_REVIEW",
                    "c",
                ),
            ],
            roots,
        )
    )
    assert len(merged_roots) == 2
    assert len(merged_findings) == 2
    unbounded = next(
        item
        for item in merged_roots
        if item.root_type == "UNBOUNDED_LIABILITY_EXPOSURE"
    )
    assert unbounded.check_code == "LRE-003"
    assert len(unbounded.source_candidate_ids) == 2
    assert len(
        next(
            item
            for item in merged_findings
            if item.finding_local_id == unbounded.finding_local_id
        ).evidence_candidates
    ) == 2
    assert set(replacements) == {finding_a_id, finding_b_id}
    assert any(
        item.root_type == "CUMULATIVE_REMEDIES_REVIEW"
        for item in merged_roots
    )


def test_equivalent_same_root_findings_are_merged_without_losing_evidence() -> None:
    finding_a_id = "finding-" + "a" * 32
    finding_b_id = "finding-" + "b" * 32

    def finding(finding_id: str, suffix: str) -> FindingDraft:
        text = f"confidential evidence {suffix}"
        return FindingDraft(
            finding_local_id=finding_id,
            source_unit_id="ip_confidentiality_data",
            domain="ip_confidentiality_data",
            check_code="ICD-004",
            category="CONFIDENTIALITY",
            risk_type="CONFIDENTIALITY_SCOPE_DEFICIENCY",
            risk_level="HIGH",
            title="Confidentiality scope is incomplete",
            issue="The same confidentiality boundary is incomplete.",
            impact_to_our_party="Our confidential information may be exposed.",
            suggestion="Define confidential information and exclusions.",
            perspective="PARTY_A",
            our_party="Party A",
            counterparty="Party B",
            evidence_candidates=[
                EvidenceCandidate(
                    evidence_local_id="evidence-" + suffix * 32,
                    finding_local_id=finding_id,
                    evidence_type="TEXT_QUOTE",
                    source_ir_item_id=f"ir-{suffix}",
                    anchor_id="anchor-shared",
                    block_id="block-shared",
                    char_start=0,
                    char_end=len(text),
                    quoted_text=text,
                    quoted_text_hash="sha256:"
                    + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            ],
        )

    def root(root_id: str, finding_id: str, suffix: str) -> CanonicalRiskRoot:
        source_id = "risk-es-" + suffix * 32
        return CanonicalRiskRoot(
            root_id=root_id,
            domain="ip_confidentiality_data",
            check_code="ICD-004",
            risk_type="CONFIDENTIALITY_SCOPE_DEFICIENCY",
            root_type="CONFIDENTIALITY_SCOPE_DEFICIENCY",
            source_candidate_ids=["risk-candidate-" + suffix * 32],
            primary_evidence_source_ids=[source_id],
            core_primary_evidence_source_ids=[source_id],
            severity_factors=CandidateSeverityFactors(missing_core_mechanism=True),
            recommended_control_codes=["DEFINE_CONFIDENTIAL_INFORMATION"],
            root_severity_rule_id="ICD_CONFIDENTIALITY_SCOPE_V1",
            risk_level="HIGH",
            finding_local_id=finding_id,
        )

    merged_findings, merged_roots, replacements = (
        _merge_equivalent_same_root_findings(
            [finding(finding_a_id, "a"), finding(finding_b_id, "b")],
            [
                root("risk-root-" + "1" * 32, finding_a_id, "1"),
                root("risk-root-" + "2" * 32, finding_b_id, "2"),
            ],
        )
    )

    assert len(merged_findings) == 1
    assert len(merged_roots) == 1
    assert replacements == {
        finding_a_id: (finding_a_id,),
        finding_b_id: (finding_a_id,),
    }
    assert len(merged_findings[0].evidence_candidates) == 2
    assert merged_roots[0].source_candidate_ids == [
        "risk-candidate-" + "1" * 32,
        "risk-candidate-" + "2" * 32,
    ]
    assert len(merged_roots[0].primary_evidence_source_ids) == 2

    retained_findings, retained_roots, retained_replacements = (
        _merge_equivalent_same_root_findings(
            [finding(finding_a_id, "a"), finding(finding_b_id, "b")],
            [
                root("risk-root-" + "1" * 32, finding_a_id, "1"),
                root("risk-root-" + "2" * 32, finding_b_id, "2").model_copy(
                    update={
                        "root_type": "CONFIDENTIALITY_RETENTION_DEFICIENCY",
                        "root_severity_rule_id": "ICD_CONFIDENTIALITY_RETENTION_V1",
                    }
                ),
            ],
        )
    )
    assert len(retained_findings) == 2
    assert len(retained_roots) == 2
    assert retained_replacements == {}

    shared_findings, shared_roots, shared_replacements = (
        _merge_equivalent_same_root_findings(
            [finding(finding_a_id, "a")],
            [
                root("risk-root-" + "1" * 32, finding_a_id, "1"),
                root("risk-root-" + "2" * 32, finding_a_id, "2").model_copy(
                    update={
                        "root_type": "CONFIDENTIALITY_RETENTION_DEFICIENCY",
                        "root_severity_rule_id": "ICD_CONFIDENTIALITY_RETENTION_V1",
                    }
                ),
            ],
        )
    )
    assert len(shared_findings) == 2
    assert len(shared_roots) == 2
    assert {
        item.finding_local_id for item in shared_roots
    } == {item.finding_local_id for item in shared_findings}
    assert len(shared_replacements[finding_a_id]) == 2
