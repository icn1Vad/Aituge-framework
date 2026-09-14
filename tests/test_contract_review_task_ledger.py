from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.risk.playbooks import PlaybookRegistry, build_default_registry
from contract.risk.review_ledger import CheckTaskScope, build_review_records
from risk_test_data import risk_plan_input
from services.contract.capabilities.risk_review import (
    CheckCoverageResult, CommercialFinancialDirectReviewer, CommercialReviewRequest,
    _prompt,
)
from test_contract_risk_direct_review import _completion, _request, _valid_payload


@pytest.mark.parametrize("count", [1, 3, 9, 13])
def test_commercial_response_size_follows_assignment_not_eight(count):
    request = _request()
    # Custom identifiers are a configured assignment, not invented model output.
    codes = [f"CF-{100 + index:03d}" for index in range(count)]
    request = CommercialReviewRequest.model_validate({
        **request.model_dump(mode="json"),
        "assigned_check_specs": [request.assigned_check_specs[0].model_copy(update={"check_code": code}).model_dump(mode="json") for code in codes],
    })
    response = {"check_results": [{"check_code": code, "status": "REVIEWED",
                "decision_note": "已检查本次分配的价款证据，未见口径冲突。", "findings": []} for code in reversed(codes)]}
    from test_seven_domain_evidence_protocol import wire_fixture, catalog_for
    response = wire_fixture(response, catalog_for(request))
    prompt = json.loads(_prompt(request)[0].split('\n', 1)[1])
    assert 'cf005_candidate' not in prompt
    assert 'cf005_required_fields' not in prompt['output_contract']
    class Runtime:
        calls = 0
        async def complete_with_usage(self, **kwargs):
            self.calls += 1
            assert "恰好8项" not in kwargs["messages"][0]["content"]
            return _completion(json.dumps(response, ensure_ascii=False))
    runtime = Runtime()
    result = asyncio.run(CommercialFinancialDirectReviewer(runtime_factory=lambda _: runtime).review(
        request, tenant_id="test", model_id="offline", framework_run_id="offline-ledger"))
    assert result.status == "COMPLETED"
    assert [check.check_code for check in result.check_results] == codes
    assert runtime.calls == 1


@pytest.mark.parametrize("count", [1, 13])
def test_registry_and_plan_accept_configured_financial_catalogue(count):
    registry = build_default_registry()
    checks = [check for check in registry.checks if check.domain != "commercial_financial"]
    template = registry.check("CF-001")
    checks.extend(template.model_copy(update={"check_code": f"CF-{100 + index:03d}"}) for index in range(count))
    manifests = [manifest.model_copy(update={"check_codes": [check.check_code for check in checks]})
                 for manifest in registry.manifests if manifest.playbook_id == "base_neutral"]
    planner = RiskReviewPlanBuilder(registry=PlaybookRegistry(manifests=manifests, checks=checks))
    plan = planner.build(risk_plan_input())
    contexts = [c for c in plan.contexts if c.unit_id == "commercial_financial"]
    assert {s.check_code for c in contexts for s in c.check_specs} == {f"CF-{100 + index:03d}" for index in range(count)}
    assert all(scope.complete for c in contexts for scope in c.check_task_scopes)


def _ledger_fixture(*, statuses, reasons=None, partial=False, missing=False, finding=False):
    check_code = "CF-001"
    scopes = [CheckTaskScope(check_code=check_code, expected_item_ids=["i1", "i2"] if partial else ["i1"],
              provided_item_ids=[f"i{index + 1}"] if partial else ["i1"],
              expected_anchor_ids=["a1", "a2"] if partial else ["a1"],
              provided_anchor_ids=[f"a{index + 1}"] if partial else ["a1"])
              for index in range(len(statuses))]
    spec = NS(check_code=check_code, review_question="价款口径是否明确")
    contexts = [NS(unit_id="commercial_financial", batch_id=f"batch-{i}", check_specs=[spec],
                   check_task_scopes=[scope], context_hash=f"hash-{i}") for i, scope in enumerate(scopes)]
    batches = [NS(batch_id=c.batch_id, model_call_count=1,
        check_results=[CheckCoverageResult(check_code=check_code, status=status,
            reason_code=(reasons[i] if reasons else "CHECK_FAILED" if status == "FAILED" else "NO_RISK_IDENTIFIED"),
            decision_note=f"依据原文完成第{i}批记录。", finding_local_ids=["f1"] if finding and i == 0 else [])],
        findings=[NS(check_code=check_code, finding_local_id="f1", evidence_candidates=[NS(anchor_id="a1")])] if finding and i == 0 else [])
        for i, (c, status) in enumerate(zip(contexts, statuses))]
    plan = NS(review_id="r", generation_id="g", plan_id="p", contexts=contexts,
              review_units=[NS(unit_id="commercial_financial", check_specs=[spec])])
    return build_review_records(plan, batches[:-1] if missing else batches)


def test_record_complete_no_risk_requires_joint_scope_and_completed_check():
    record, = _ledger_fixture(statuses=["REVIEWED"])
    assert record.judgement == "NO_RISK" and not record.unresolved_reasons
    assert record.provided_anchor_ids == ["a1"]


def test_successful_shard_does_not_hide_failed_sibling():
    record, = _ledger_fixture(statuses=["REVIEWED", "FAILED"])
    assert record.execution_status == "PARTIAL"
    assert record.judgement == "UNRESOLVED"
    assert "CHECK_EXECUTION_FAILED" in record.unresolved_reasons


def test_local_no_risk_on_every_shard_is_not_a_global_clearance():
    record, = _ledger_fixture(statuses=["REVIEWED", "REVIEWED"], partial=True)
    assert record.scope_complete
    assert record.judgement == "INSUFFICIENT_EVIDENCE"
    assert "CROSS_SHARD_SYNTHESIS_REQUIRED" in record.unresolved_reasons


def test_record_keeps_valid_finding_when_other_part_failed():
    record, = _ledger_fixture(statuses=["REVIEWED", "FAILED"], finding=True)
    assert record.judgement == "RISK"
    assert record.execution_status == "PARTIAL"
    assert record.finding_local_ids == ["f1"] and record.cited_anchor_ids == ["a1"]
    assert record.unresolved_reasons


def test_missing_assigned_output_is_recorded_not_cleared():
    record, = _ledger_fixture(statuses=["REVIEWED", "REVIEWED"], missing=True)
    assert "ASSIGNED_TASK_NOT_RETURNED" in record.unresolved_reasons
    assert record.judgement != "NO_RISK"


def test_explicit_insufficient_evidence_is_not_no_risk():
    record, = _ledger_fixture(statuses=["REVIEWED"], reasons=["INSUFFICIENT_EVIDENCE"])
    assert record.judgement == "INSUFFICIENT_EVIDENCE"


def test_scope_rejects_foreign_evidence():
    with pytest.raises(ValueError, match="frozen scope"):
        CheckTaskScope(check_code="CF-001", expected_anchor_ids=["a1"], provided_anchor_ids=["foreign"])
