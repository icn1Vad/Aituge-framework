import asyncio
import json
from types import SimpleNamespace
import pytest

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.rule_evidence.execution import RuleLibraryExecution
from contract.rule_evidence.reviewer import RuleLibraryReviewer
from contract.rule_evidence.shadow import RuleLibraryShadow
from contract.evidence_planning.review_result import RuleReviewResult
from risk_test_data import risk_plan_input
from test_rule_library_shadow import snapshot


class FakeModel:
    def __init__(self, corrupt=False):
        self.calls = 0
        self.corrupt = corrupt

    async def complete_with_usage(self, **kwargs):
        self.calls += 1
        data = json.loads(kwargs["messages"][0]["content"])
        decisions = []
        for task in data["rules"]:
            source = next(iter(data["contract_sources"]), None)
            decisions.append({"evidence_id": task["evidence_id"],
                              "outcome": "RISK" if source else "INSUFFICIENT_EVIDENCE",
                              "title": task["rule_name"], "reason": "按分配规则审查付款条件",
                              "suggestion": "明确付款期限与验收条件",
                              "primary_evidence_source_ids": ["unknown-contract-source" if self.corrupt else source["source_id"]]
                              if source else []})
        return SimpleNamespace(content=json.dumps({"decisions": decisions}, ensure_ascii=False),
                               prompt_tokens=100, completion_tokens=80)


def test_existing_rules_actually_enter_model_and_produce_traceable_decisions(tmp_path):
    value = risk_plan_input()
    runtime = FakeModel()
    execution = RuleLibraryExecution(snapshot(tmp_path),
                                    tmp_path / "cache", runtime_factory=lambda tenant: runtime)
    async def run():
        return await execution.run(value, "42", "test-model", contract_type_name="采购合同", business_role="买受方")
    first = asyncio.run(run())
    assert first.status == "COMPLETED"
    assert first.decisions and first.decisions[0].outcome == "RISK"
    assert first.evidence[0].source_status == "pending" and first.mode == "PREVIEW"
    assert first.decisions[0].citations[0].quoted_text
    calls = runtime.calls
    assert asyncio.run(run()) == first
    assert runtime.calls == calls == 1
    assert first.prompt_tokens == 100


def test_invented_source_ids_are_rejected_without_retry_or_fabricated_success(tmp_path):
    value = risk_plan_input()
    plan = RiskReviewPlanBuilder().build(value)
    observation = RuleLibraryShadow(snapshot(tmp_path)).evaluate(
        plan, tenant_id="42", contract_type_name="采购合同", business_role="买受方")
    runtime = FakeModel(corrupt=True)
    result = asyncio.run(RuleLibraryReviewer(runtime).review(observation, plan, tenant_id="42", model_id="test"))
    assert result.status == "PARTIAL" and not result.decisions
    assert result.pending_evidence_ids and runtime.calls == 1
    assert result.diagnostics == ["UNKNOWN_CONTRACT_SOURCE:1"]
    with pytest.raises(ValueError, match="Preview bundle"):
        asyncio.run(RuleLibraryReviewer(runtime).review(observation, plan, tenant_id="42", model_id="test", mode="ACTIVE"))


def test_result_does_not_accept_unresolved_or_missing_rule_references(tmp_path):
    value = risk_plan_input()
    execution = RuleLibraryExecution(snapshot(tmp_path), tmp_path / "cache", runtime_factory=lambda tenant: FakeModel())
    result = asyncio.run(execution.run(value, "42", "test", contract_type_name="采购合同", business_role="买受方"))
    payload = result.model_dump()
    payload["decisions"][0]["evidence_id"] = "rule-evidence-" + "f" * 32
    with pytest.raises(ValueError):
        RuleReviewResult.model_validate(payload)
