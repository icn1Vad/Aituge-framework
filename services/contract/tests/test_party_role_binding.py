from __future__ import annotations

from types import SimpleNamespace

from services.contract.capabilities.party_roles import contract_party_roles
from services.contract.capabilities.risk_review import (
    CommercialIrItem,
    ModelCheckCoverageResult,
    ModelEvidenceDraft,
    ModelFindingDraft,
    _build_cf005_candidate,
    _suppress_cf005_perspective_conflict,
)
from services.contract.capabilities.risk_review_bundle import (
    _po_item_burdens_our_party,
    _validate_po_perspective_language,
)
from services.contract.scripts.contract_risk_stage66_direct_e2e import (
    _compatibility_context,
)


def _request(
    perspective: str,
    *,
    our_party: str,
    counterparty: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        perspective=perspective,
        our_party=our_party,
        counterparty=counterparty,
    )


def _item(
    *,
    ir_type: str,
    item_id: str,
    subject: str,
    predicate: str,
    object_: str,
) -> CommercialIrItem:
    return CommercialIrItem(
        ir_type=ir_type,
        item_id=item_id,
        subject=subject,
        predicate=predicate,
        object=object_,
        source_anchors=[{"anchor_id": f"anchor-{item_id}"}],
    )


def test_contract_party_roles_are_symmetric_for_both_perspectives() -> None:
    party_a_view = contract_party_roles(
        perspective="PARTY_A",
        our_party="甲公司",
        counterparty="乙公司",
    )
    assert party_a_view.party_a_name == "甲公司"
    assert party_a_view.party_b_name == "乙公司"
    assert party_a_view.our_role == "甲方"
    assert party_a_view.counterparty_role == "乙方"

    party_b_view = contract_party_roles(
        perspective="PARTY_B",
        our_party="乙公司",
        counterparty="甲公司",
    )
    assert party_b_view.party_a_name == "甲公司"
    assert party_b_view.party_b_name == "乙公司"
    assert party_b_view.our_role == "乙方"
    assert party_b_view.counterparty_role == "甲方"


def test_compatibility_context_does_not_turn_party_b_into_party_a() -> None:
    value = SimpleNamespace(
        review_id="review-1",
        document_id="document-1",
        generation_id="generation-1",
        contract_type="SERVICE",
        perspective="PARTY_B",
        our_party="乙公司",
        counterparty="甲公司",
        review_attitude="NEUTRAL",
    )

    context = _compatibility_context(
        value, "sha256:" + "1" * 64
    )

    assert context.contract_profile.party_a.name == "甲公司"
    assert context.contract_profile.party_b.name == "乙公司"
    assert context.contract_profile.our_party == "乙公司"
    assert context.contract_profile.counterparty == "甲公司"


def test_po_directional_evidence_is_adverse_only_to_the_burdened_side() -> None:
    provider_duty = _item(
        ir_type="obligations",
        item_id="provider-duty",
        subject="乙方",
        predicate="须按甲方要求完成",
        object_="项目任务",
    )
    customer_control = _item(
        ir_type="rights",
        item_id="customer-control",
        subject="甲方",
        predicate="有权单方调整",
        object_="范围和工期",
    )
    party_a_request = _request(
        "PARTY_A", our_party="甲公司", counterparty="乙公司"
    )
    party_b_request = _request(
        "PARTY_B", our_party="乙公司", counterparty="甲公司"
    )

    assert not _po_item_burdens_our_party(
        party_a_request, provider_duty
    )
    assert not _po_item_burdens_our_party(
        party_a_request, customer_control
    )
    assert _po_item_burdens_our_party(
        party_b_request, provider_duty
    )
    assert _po_item_burdens_our_party(
        party_b_request, customer_control
    )


def test_cf005_payer_role_follows_perspective() -> None:
    payment = _item(
        ir_type="payment_terms",
        item_id="payment",
        subject="甲方",
        predicate="应在合同签订后支付",
        object_="全部价款",
    )
    ir_refs = {"I001": payment}
    anchors = {"anchor-payment": "A001"}

    party_a_candidate = _build_cf005_candidate(
        _request("PARTY_A", our_party="甲公司", counterparty="乙公司"),
        ir_refs,
        anchors,
    )
    party_b_candidate = _build_cf005_candidate(
        _request("PARTY_B", our_party="乙公司", counterparty="甲公司"),
        ir_refs,
        anchors,
    )

    assert party_a_candidate.payer_role_status == "OUR_PARTY"
    assert party_b_candidate.payer_role_status == "COUNTERPARTY"


def test_cf005_ignores_breach_payment_when_identifying_the_prepayment_payer() -> None:
    prepayment = _item(
        ir_type="payment_terms",
        item_id="prepayment",
        subject="甲方",
        predicate="应在合同生效后10个工作日内支付",
        object_="合同金额的50%",
    )
    breach_penalty = _item(
        ir_type="payment_terms",
        item_id="breach-penalty",
        subject="乙方",
        predicate="应按合同总金额的1%向甲方支付违约金",
        object_="未按期完成服务",
    )

    candidate = _build_cf005_candidate(
        _request("PARTY_B", our_party="乙公司", counterparty="甲公司"),
        {"I001": prepayment, "I002": breach_penalty},
        {"anchor-prepayment": "A001", "anchor-breach-penalty": "A002"},
    )

    assert candidate.payer_role_status == "COUNTERPARTY"
    assert candidate.candidate_ir_refs == ["I001"]
    assert candidate.candidate_evidence_refs == ["A001"]


def test_cf005_perspective_conflict_excludes_only_the_reversed_finding() -> None:
    prepayment = _item(
        ir_type="payment_terms",
        item_id="prepayment",
        subject="甲方",
        predicate="应在合同生效后10个工作日内支付",
        object_="合同金额的50%",
    )
    candidate = _build_cf005_candidate(
        _request("PARTY_B", our_party="乙公司", counterparty="甲公司"),
        {"I001": prepayment},
        {"anchor-prepayment": "A001"},
    )
    original = ModelCheckCoverageResult(
        check_code="CF-005",
        status="REVIEWED",
        reason_code="RISK_IDENTIFIED",
        decision_note="模型错误地把对方付款的预付款风险归到了我方",
        candidate_decision="RISK_CONFIRMED",
        findings=[
            ModelFindingDraft(
                check_code="CF-005",
                category="PAYMENT",
                risk_type="PREPAYMENT_WITHOUT_PERFORMANCE_SECURITY",
                risk_level="HIGH",
                title="错误的预付款风险",
                issue="付款方是对方，不应认定为我方预付款风险",
                impact_to_our_party="不应进入我方修订草案",
                suggestion="不生成该建议",
                evidence=[
                    ModelEvidenceDraft(
                        evidence_type="TEXT_QUOTE",
                        ir_ref="I001",
                        evidence_ref="A001",
                    )
                ],
            )
        ],
    )

    sanitized, warning = _suppress_cf005_perspective_conflict(original, candidate)

    assert warning == "RISK_PERSPECTIVE_CONFLICT"
    assert sanitized.status == "REVIEWED"
    assert sanitized.reason_code == "NO_RISK_IDENTIFIED"
    assert sanitized.candidate_decision == "PERSPECTIVE_REJECTED"
    assert sanitized.findings == []
    assert "RISK_PERSPECTIVE_CONFLICT" in sanitized.decision_note


def test_po_perspective_conflict_is_reported_to_the_materializer_without_raising() -> None:
    reversed_finding = SimpleNamespace(
        title="立场错误",
        issue="我方为甲方时应承担全部付款义务",
        impact_to_our_party="反向表述不应生成修订草案",
        suggestion="删除该错误风险",
    )

    assert _validate_po_perspective_language(
        _request("PARTY_B", our_party="乙公司", counterparty="甲公司"),
        reversed_finding,
    )
