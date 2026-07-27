from __future__ import annotations

from types import SimpleNamespace

from services.contract.capabilities.party_roles import contract_party_roles
from services.contract.capabilities.risk_review import (
    CommercialIrItem,
    _build_cf005_candidate,
)
from services.contract.capabilities.risk_review_bundle import (
    _po_item_burdens_our_party,
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
