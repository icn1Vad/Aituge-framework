"""No paid providers: partial execution through final payload and callback."""
import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

from contract.api.models import PublicReviewResultData
from contract.application.result_hash import compute_result_hash
from services.contract.capabilities.direct_e2e import build_formal_result, build_final_callback, DryRunResultSink
from services.contract.capabilities.review_completion import review_completion, result_overview, final_result_overview
from test_direct_e2e import _compatible, _context, _request, BLOCK

UNITS = ["formation_validity_authority", "commercial_financial", "performance_obligations",
         "ip_confidentiality_data", "liability_remedies_exit", "cross_clause_consistency", "missing_ambiguity_completeness"]
CODES = ["FVA-002", "CF-005", "PO-001", "ICD-001", "LRE-001", "CCC-001", "MAC-001"]


def extended(failed=None, *, uncertain=False):
    records, units = [], []
    for name, code in zip(UNITS, CODES):
        bad = name == failed
        status = "FAILED" if bad else "COMPLETED"
        check = NS(check_code=code, status="FAILED" if bad else "REVIEWED",
                   reason_code="INSUFFICIENT_EVIDENCE" if bad else "NO_RISK_IDENTIFIED")
        units.append(NS(unit_id=name, status=status, check_results=[check], call_metrics=[], batch_metrics=[],
                        model_dump=lambda **kw: {}))
        records.append(NS(check_code=code, unit_id=name, review_question=f"{name}检查问题",
                          execution_status=status, judgement="UNRESOLVED" if bad else "NO_RISK",
                          batch_ids=["batch-"+name], unresolved_reasons=["CHECK_EXECUTION_FAILED"] if bad else []))
    if uncertain:
        records[0].unresolved_reasons = ["CROSS_SHARD_SYNTHESIS_REQUIRED"]
        records[0].judgement = "INSUFFICIENT_EVIDENCE"
    base = NS(units=units[:5], status="PARTIAL_FAILED" if failed in UNITS[:5] else "COMPLETED")
    base.model_dump = lambda **kw: {"status": base.status}
    value = NS(status="PARTIAL_FAILED" if failed else "COMPLETED", base_bundle=base,
               horizontal_units=units[5:], review_records=records, check_codes=CODES,
               metrics=NS(extended_bundle_wall_ms=1, repair_count=0, base_peak_concurrency=1, horizontal_peak_concurrency=1))
    value.model_dump = lambda **kw: {"status": value.status}
    return value


@pytest.mark.parametrize("unit", UNITS)
def test_each_failed_domain_preserves_validated_findings_and_discloses_scope(unit):
    value = extended(unit)
    before = copy.deepcopy([(r.execution_status, r.judgement) for r in value.review_records])
    completion = review_completion(value)
    assert completion["status"] == "PARTIAL"
    assert completion["pending_check_count"] == 1
    assert completion["completed_check_count"] == 6
    assert completion["pending_checks"][0]["unit_id"] == unit
    assert before == [(r.execution_status, r.judgement) for r in value.review_records]
    compatible = _compatible()
    formal, payload, _ = build_formal_result(compatible, context=_context(), generation_id="generation-test",
                                           framework_task_id="task-test", framework_run_id="run-test",
                                           review_completion=completion)
    assert payload.findings == compatible.final_findings
    assert payload.evidences == compatible.final_evidence
    public = PublicReviewResultData.from_internal(payload)
    assert "部分审查待完成" in public.summary.overview
    assert "未完成部分不代表无风险" in public.summary.overview
    assert unit+"检查问题" in public.summary.overview
    raw = formal.model_dump(mode="json"); raw.pop("result_type")
    assert compute_result_hash(raw)[0] == payload.result_hash
    sink = DryRunResultSink()
    assert sink.submit(payload.model_dump(mode="json")).status == "ACCEPTED"
    assert sink.submit(payload.model_dump(mode="json")).status == "DUPLICATE"
    assert sink.callback(build_final_callback(formal, framework_task_id="task-test", framework_run_id="run-test")).accepted


def test_empty_partial_result_never_claims_clearance_and_hash_covers_pending_scope():
    compatible = _compatible().model_copy(update={"final_findings": [], "final_evidence": []})
    args = dict(context=_context(), generation_id="generation-test", framework_task_id="t", framework_run_id="r")
    _, partial, _ = build_formal_result(compatible, review_completion=review_completion(extended(UNITS[1])), **args)
    _, complete, _ = build_formal_result(compatible, **args)
    assert partial.findings == []
    assert "未发现需要人工复核" not in partial.summary.overview
    assert "0项有依据" in partial.summary.overview
    assert partial.result_hash != complete.result_hash


def test_missing_records_and_insufficient_or_fragmented_scope_are_not_complete():
    for value in [extended(uncertain=True), extended()]:
        if not value.review_records[0].unresolved_reasons:
            value.review_records.pop(0)
        assert review_completion(value)["pending_check_count"] == 1
    assert review_completion(extended())["status"] == "COMPLETED"
    assert result_overview(1, review_completion(extended())) == result_overview(1, None)


def test_final_overview_counts_merged_cards_and_keeps_rule_pending_state():
    completion = review_completion(extended(UNITS[1]))
    result = {"findings": [{} for _ in range(17)], "rule_review": {
        "status": "PARTIAL", "pending_evidence_ids": ["r1"],
        "decisions": [{"evidence_id": "r2", "outcome": "INSUFFICIENT_EVIDENCE"},
                      {"evidence_id": "r3", "outcome": "RISK"}]}}
    before = copy.deepcopy(result)
    overview = final_result_overview(result, completion)
    assert "已保留17项" in overview
    assert "1个检查项待复核" in overview
    assert "规则库审查另有2项待复核" in overview
    assert result == before


@pytest.mark.parametrize("rule_status", ["PARTIAL", "SELECTION_UNRESOLVED", "COMPLETED"])
def test_zero_cards_with_pending_rule_review_never_claims_no_risk(rule_status):
    result = {"findings": [], "rule_review": {"status": rule_status,
        "decisions": [{"evidence_id": "r1", "outcome": "INSUFFICIENT_EVIDENCE"}]}}
    overview = final_result_overview(result, review_completion(extended()))
    assert "未发现需要人工复核的实质合同风险" not in overview
    assert "1项待复核" in overview


@pytest.mark.parametrize("failed_unit", [UNITS[1], UNITS[-1]])
def test_real_orchestrator_continues_after_base_or_horizontal_partial(monkeypatch, tmp_path, failed_unit):
    from services.contract.scripts import contract_risk_stage66_direct_e2e as script
    from services.contract.capabilities import horizontal_review as hz
    bundle = extended(failed_unit)
    compatible = _compatible()
    projection = NS(artifacts=compatible.legacy_artifacts, routing_records=[])
    calls = []
    async def base(*args, **kwargs):
        calls.append("base"); return bundle.base_bundle
    async def horizontal(*args, **kwargs):
        calls.append("horizontal"); return bundle.horizontal_units, 1
    async def merge(*args, **kwargs):
        calls.append("merge")
        return dict(artifact=dict(result_type="FINDING_CONSOLIDATION_V1", status="COMPLETED",
                                 candidate_count=0, model_call_count=0, decisions=[], skip_reason=None),
                    call_metrics=[], wall_duration_ms=0)
    monkeypatch.setenv("CONTRACT_REVIEW_DIAGNOSTIC_DIR", str(tmp_path))
    monkeypatch.setattr(script, "RiskReviewPlanBuilder", lambda: NS(build=lambda value: NS(review_units=[], plan_id="p", plan_hash="h")))
    monkeypatch.setattr(script, "execute_base_risk_review_bundle", base)
    monkeypatch.setattr(script, "build_horizontal_plan", lambda *a, **kw: NS())
    monkeypatch.setattr(script, "execute_horizontal_phase", horizontal)
    monkeypatch.setattr(hz, "build_horizontal_review_records", lambda *a: [])
    monkeypatch.setattr(script, "build_extended_bundle", lambda **kw: bundle)
    monkeypatch.setattr(script, "_validate_oracle", lambda value: [])
    monkeypatch.setattr(script, "LegacyRiskArtifactAdapter", lambda: NS(adapt=lambda value: projection))
    monkeypatch.setattr(script, "_finding_contexts", lambda value: {})
    monkeypatch.setattr(script, "build_candidate_pairs", lambda *a, **kw: [])
    monkeypatch.setattr(script, "FindingConsolidationEngine", lambda: NS(consolidate_with_metrics=merge))
    monkeypatch.setattr(script, "finalize_legacy_compatible_result", lambda *a, **kw: compatible)
    monkeypatch.setattr(script, "_core_signature", lambda value: "sha256:"+"a"*64)
    monkeypatch.setattr(script, "_core_components", lambda value: [])
    summary, attempt, payload, _, returned = asyncio.run(script._execute_one(
        run_index=1, value=NS(review_id="review-test", source_blocks=[NS(model_dump=lambda **kw: BLOCK)]),
        request=_request(), context=_context(), tenant_id="test", model_id="fake",
        allow_dynamic_base_batch_count=True, diagnostic_allow_oracle_drift=True))
    assert calls == ["base", "horizontal", "merge"]
    assert summary["review_completion"]["status"] == "PARTIAL"
    assert summary["review_completion"]["pending_check_count"] == 1
    assert payload.findings == compatible.final_findings
    assert returned.status == "PARTIAL_FAILED"
    assert summary["duplicate_callback_status"] == "DUPLICATE"
    assert summary["replay"]["repetitions"] == 100
