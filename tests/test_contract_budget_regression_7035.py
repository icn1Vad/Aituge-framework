"""Regression for the real 13009 PO failure; no external model calls."""
import asyncio
import json
from dataclasses import replace

from test_contract_risk_base_bundle import FakeRuntime, _completion, _po_candidate_payload, _po_request
from services.contract.capabilities.risk_review_bundle import GenericBaseDirectReviewer, _apply_prompt_budget


def test_po_7035_retains_candidates_and_records_warning_without_retry():
    request = _po_request(po003_mode="PARTY_A_SELF")
    completion = _completion(json.dumps(_po_candidate_payload(request), ensure_ascii=False), "performance_obligations")
    completion = replace(completion, prompt_tokens=7035, total_tokens=7035 + (completion.completion_tokens or 0))
    runtime = FakeRuntime([completion])
    result = asyncio.run(GenericBaseDirectReviewer(runtime_factory=lambda _: runtime).review(
        request, tenant_id="tenant-1", model_id="fake-model"))
    result = _apply_prompt_budget(request, result, legal_evidence_input_tokens=request.legal_evidence_input_tokens)
    assert result.status == "COMPLETED"
    assert len(result.check_results) == len(request.assigned_check_specs)
    assert result.candidate_decisions
    assert len(runtime.calls) == 1
    assert result.repair_count == 0
    assert result.prompt_budget.provider_prompt_tokens == 7035
    assert result.prompt_budget.tokens_over_hard_limit == 35
    assert result.prompt_budget.budget_status == "SOFT_WARNING"
    assert "RISK_PROMPT_TOKEN_TARGET_EXCEEDED" in result.warnings
