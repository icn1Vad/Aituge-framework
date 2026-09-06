"""Offline regression for the 13009 17,694-token / 24-source failure.

Synthetic source locations and fake completions: no external model calls.
"""
import asyncio
import json
from dataclasses import replace

import pytest

from test_contract_risk_base_bundle import (
    FakeRuntime, _completion, _po_candidate_payload, _po_request,
)
from services.contract.capabilities.risk_review_bundle import (
    DeterministicRiskCandidate, GenericBaseDirectReviewer, GenericReviewRequest,
    _apply_prompt_budget, _build_generic_candidates, _generic_prompt,
)


def many_scope_sources(count):
    request = _po_request()
    _, ir_refs, anchors = _generic_prompt(request)
    candidates = _build_generic_candidates(request, ir_refs, {x.anchor_id: ref for ref, x in anchors.items()})
    initial_count = len(next(x for x in candidates if x.candidate_type == "SCOPE_EXPANSION").primary_evidence_source_ids)
    payload = request.model_dump(mode="json")
    item = next(x for x in payload["projected_ir_items"] if x["item_id"] == "ir-po-scope")
    source = next(x for x in payload["evidence_sources"] if x["ir_item_id"] == item["item_id"])
    excerpt = next(x for x in payload["source_excerpts"] if x["anchor_id"] == source["anchor_id"])
    for index in range(1, count - initial_count + 1):
        anchor_id, block_id = f"scope-anchor-{index}", f"scope-block-{index}"
        new_source = dict(source, source_id=f"risk-es-{index:032x}", anchor_id=anchor_id, block_id=block_id)
        payload["evidence_sources"].append(new_source)
        payload["source_excerpts"].append(dict(excerpt, anchor_id=anchor_id, block_id=block_id, block_no=100 + index))
        item["source_anchors"].append({"anchor_id": anchor_id})
        for policy in payload["check_evidence_policies"]:
            if policy["check_code"] in source["allowed_check_codes"]:
                policy["allowed_evidence_source_ids"].append(new_source["source_id"])
    return GenericReviewRequest.model_validate(payload)


@pytest.mark.parametrize("source_count", [24, 64])
@pytest.mark.parametrize("prompt_tokens", [7489, 17694])
def test_large_source_set_survives_candidate_decision_root_and_finding(source_count, prompt_tokens):
    request = many_scope_sources(source_count)
    payload = _po_candidate_payload(request, risk_check_code="PO-001", risk_candidate_type="SCOPE_EXPANSION")
    completion = replace(
        _completion(json.dumps(payload, ensure_ascii=False), request.unit_id),
        prompt_tokens=prompt_tokens, total_tokens=prompt_tokens + 500,
    )
    runtime = FakeRuntime([completion])
    result = asyncio.run(GenericBaseDirectReviewer(runtime_factory=lambda _: runtime).review(
        request, tenant_id="tenant-1", model_id="fake-model"))
    result = _apply_prompt_budget(request, result)
    assert result.status == "COMPLETED"
    assert len(runtime.calls) == 1
    assert result.repair_count == 0
    assert result.prompt_budget.policy_version == "3.0"
    assert result.prompt_budget.hard_limit_enforced is False
    assert result.prompt_budget.provider_prompt_tokens == prompt_tokens
    assert result.prompt_budget.budget_status == "SOFT_WARNING"
    decision = next(x for x in result.candidate_decisions if x.candidate_type == "SCOPE_EXPANSION")
    expected = set(decision.primary_evidence_source_ids)
    assert len(expected) == source_count
    assert any(expected <= set(x.primary_evidence_source_ids) for x in result.canonical_risk_roots)
    expected_anchors = {s.anchor_id for s in request.evidence_sources if s.source_id in expected}
    assert any(expected_anchors <= {e.anchor_id for e in x.evidence_candidates} for x in result.findings)


def test_removing_cardinality_cap_keeps_duplicate_and_role_validation():
    request = many_scope_sources(24)
    _, ir_refs, anchors = _generic_prompt(request)
    candidates = _build_generic_candidates(request, ir_refs, {x.anchor_id: ref for ref, x in anchors.items()})
    candidate = next(x for x in candidates if x.candidate_type == "SCOPE_EXPANSION")
    payload = candidate.model_dump(mode="json")
    payload["primary_evidence_source_ids"].append(payload["primary_evidence_source_ids"][0])
    with pytest.raises(ValueError, match="unique"):
        DeterministicRiskCandidate.model_validate(payload)
    payload = candidate.model_dump(mode="json")
    payload["core_primary_evidence_source_ids"] = []
    payload["context_primary_evidence_source_ids"] = []
    with pytest.raises(ValueError, match="cover all"):
        DeterministicRiskCandidate.model_validate(payload)
