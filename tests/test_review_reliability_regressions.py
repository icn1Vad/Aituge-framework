"""Offline reproductions of SIG-003 failure SHAPES; no customer text or APIs."""
import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.risk.review_ledger import CheckTaskScope
from risk_test_data import risk_plan_input
from services.contract.capabilities.commercial_output_repair import infer_repair_targets
from services.contract.capabilities.risk_review import (
    DirectReviewError, _materialize, _prompt,
    _enrich_reason_codes, _parse_model_output, _semantic_snapshot, _validate_semantic_preservation,
)
from test_contract_risk_direct_review import _request, _valid_payload, _completion, _review


@pytest.mark.parametrize("checks", [None, "invalid", 1, [None], [{"check_code": None}]])
def test_target_inference_does_not_mask_the_original_schema_error(checks):
    assert infer_repair_targets(DirectReviewError("RISK_DIRECT_SCHEMA_INVALID", "bad shape"), {"check_results": checks}) == []


def test_scope_guard_covers_generic_batches_and_preserves_quarantined_findings():
    from services.contract.capabilities.risk_review_bundle import _commercial_batch, _record_scope_limits
    result, _ = _review([_completion(json.dumps(_valid_payload()))])
    batch = _commercial_batch(_request(), result)
    original = batch.findings[0]
    # Guard must reject global absence in every domain, even if an upstream
    # legacy validator allowed it through with inconsistent source metadata.
    batch.findings[0] = original.model_copy(update={"evidence_candidates": [
        original.evidence_candidates[0].model_copy(update={"evidence_type": "ABSENCE"})]})
    context = NS(check_task_scopes=[CheckTaskScope(check_code="CF-001",
        expected_item_ids=["i1", "i2"], provided_item_ids=["i1"])])
    guarded = _record_scope_limits(context, batch)
    assert guarded.status == "PARTIAL_FAILED"
    assert not guarded.findings and not guarded.check_results[0].finding_local_ids
    assert guarded.deferred_findings[0].finding_local_id == original.finding_local_id


def test_generic_empty_note_repair_receives_sources_and_preserves_siblings():
    from test_contract_risk_base_bundle import _request as generic_request, _payload, _completion as completion, FakeRuntime
    from services.contract.capabilities.risk_review_bundle import GenericBaseDirectReviewer
    initial = _payload()
    corrected = copy.deepcopy(initial["check_results"][1])
    initial["check_results"][1]["decision_note"] = ""
    runtime = FakeRuntime([completion(json.dumps(initial), "formation_validity_authority"),
        completion(json.dumps({"check_results": [corrected]}), "formation_validity_authority", repair_no=1)])
    result = asyncio.run(GenericBaseDirectReviewer(runtime_factory=lambda _: runtime).review(
        generic_request(), tenant_id="offline", model_id="offline"))
    assert result.status == "COMPLETED"
    payload = json.loads(runtime.calls[1]["messages"][0]["content"])
    assert payload["target_check_codes"] == ["FVA-002"]
    assert "contract_evidence_catalog" in payload["source_backed_review_context"]


def test_small_packing_budget_does_not_split_one_check_into_conflicting_global_verdicts(monkeypatch):
    planner = RiskReviewPlanBuilder()
    partition = planner._partition_checks
    def small_partition(**kwargs):
        return partition(**{**kwargs, "hard_limit": 100})
    slices = planner._context_slices
    def small_slices(**kwargs):
        return slices(**{**kwargs, "hard_limit": 100})
    monkeypatch.setattr(planner, "_partition_checks", small_partition)
    monkeypatch.setattr(planner, "_context_slices", small_slices)
    plan = planner.build(risk_plan_input())
    contexts = [c for c in plan.contexts if c.unit_id == "commercial_financial"]
    codes = [s.check_code for c in contexts for s in c.check_specs]
    assert len(codes) == len(set(codes))
    assert all(scope.complete for c in contexts for scope in c.check_task_scopes)


def test_missing_assigned_check_can_be_added_without_rewriting_existing_results():
    initial = _valid_payload()
    missing = initial["check_results"].pop(1)
    result, runtime = _review([_completion(json.dumps(initial)),
        _completion(json.dumps({"check_results": [missing]}), repair_no=1)])
    assert result.status == "COMPLETED"
    assert len(result.check_results) == 8
    assert json.loads(runtime.calls[1]["messages"][0]["content"])["target_check_codes"] == ["CF-002"]
    normalized = result.attempt_diagnostics[1].normalized_output["check_results"]
    from test_seven_domain_evidence_protocol import normalized_fixture
    assert all(c in normalized for c in normalized_fixture(initial)["check_results"])


def test_contradictory_payment_verdict_and_title_are_reassessed_together():
    from test_contract_risk_direct_review import _request_with_single_payment_text
    request = _request_with_single_payment_text("验收合格后，甲方一次性支付全部合同价款。")
    initial = _valid_payload()
    wrong = initial["check_results"][4]
    finding = copy.deepcopy(initial["check_results"][0]["findings"][0])
    finding.update(check_code="CF-005", risk_type="ADVANCE_PAYMENT_SECURITY_RISK", title="错误的履约前全额付款风险")
    wrong.update(candidate_decision="RISK_CONFIRMED", decision_note="验收后付款但误判为履约前付款", findings=[finding],
                 candidate_evidence=[], identified_security_mechanisms=[])
    corrected = copy.deepcopy(wrong)
    corrected.update(candidate_decision="TRIGGER_NOT_MET", findings=[], decision_note="本项原文为验收合格后付款，不成立履约前付款触发条件")
    result, runtime = _review([_completion(json.dumps(initial)),
        _completion(json.dumps({"check_results": [corrected]}), repair_no=1)], request=request)
    assert result.status == "COMPLETED"
    assert not any(f.check_code == "CF-005" for f in result.findings)
    assert result.attempt_diagnostics[0].validation_error.startswith("RISK_CF005_TRIGGER_CONTRADICTION")
    assert json.loads(runtime.calls[1]["messages"][0]["content"])["repair_mode"] == "REASSESS_TARGET"


@pytest.mark.parametrize("failure", ["type", "pair", "ambiguous"])
def test_wrong_evidence_can_be_rebound_without_freezing_the_error(failure):
    request = _request()
    initial = _valid_payload()
    evidence = initial["check_results"][0]["findings"][0]["evidence"][0]
    if failure == "type":
        evidence["evidence_type"] = "IR"
        evidence.pop("evidence_ref")
    elif failure == "pair":
        evidence["evidence_ref"] = "A002"  # exists, but does not belong to I001
    else:
        request.projected_ir_items[0].source_anchors.append(request.projected_ir_items[1].source_anchors[0])
        evidence.pop("evidence_ref")
    repaired = copy.deepcopy(_valid_payload()["check_results"][0])
    result, runtime = _review([
        _completion(json.dumps(initial)),
        _completion(json.dumps({"check_results": [repaired]}), repair_no=1),
    ], request=request)
    assert result.status == "COMPLETED"
    assert result.model_call_count == 2
    repair = json.loads(runtime.calls[1]["messages"][0]["content"])
    assert repair["repair_mode"] == "REASSESS_TARGET"
    catalog = repair["context"]["contract_evidence_catalog"]
    assert {item['quoted_text'] for item in catalog.values()} >= {a.quoted_text for a in request.source_excerpts}
    assert all('ir_ref' not in item and 'evidence_ref' not in item for item in catalog.values())
    from test_seven_domain_evidence_protocol import normalized_fixture
    siblings = {"check_results": initial["check_results"][1:]}
    assert result.attempt_diagnostics[-1].normalized_output["check_results"][1:] == normalized_fixture(siblings, request)["check_results"]


def test_rebinding_never_accepts_a_nonexistent_anchor():
    initial = _valid_payload()
    initial["check_results"][0]["findings"][0]["evidence"][0]["evidence_type"] = "IR"
    corrected = _valid_payload()["check_results"][0]
    corrected["findings"][0]["evidence"][0]["evidence_ref"] = "A999"
    with pytest.raises(DirectReviewError) as caught:
        _review([_completion(json.dumps(initial)), _completion(json.dumps({"check_results": [corrected]}), repair_no=1)])
    assert caught.value.code == "RISK_EVIDENCE_SELECTION_INVALID"


def test_business_reassessment_can_correct_title_but_not_a_passed_sibling():
    before = _semantic_snapshot(_valid_payload())
    after = copy.deepcopy(before)
    after["CF-001"]["findings"][0]["title"] = "更正后的、与原文相符的标题"
    _validate_semantic_preservation(before, after, repair_targets=["CF-001"], mode="REASSESS_TARGET")
    with pytest.raises(DirectReviewError):
        _validate_semantic_preservation(before, after, repair_targets=["CF-002"], mode="REASSESS_TARGET")
    with pytest.raises(DirectReviewError):
        _validate_semantic_preservation(before, after, repair_targets=["CF-001"], mode="REBIND_EVIDENCE")


def test_partial_absence_is_quarantined_not_a_batch_exception_or_clearance():
    request = _request()
    request.check_task_scopes = [CheckTaskScope(check_code="CF-008",
        expected_item_ids=["i1", "i2"], provided_item_ids=["i1"])]
    payload = _valid_payload()
    finding = copy.deepcopy(payload["check_results"][0]["findings"][0])
    finding.update(check_code="CF-008", category="ACCEPTANCE", risk_type="ACCEPTANCE_RISK",
        evidence=[{"evidence_type": "ABSENCE", "checked_scope": "全文", "verification_note": "未见验收约定"}])
    payload["check_results"][7]["findings"] = [finding]
    result, runtime = _review([_completion(json.dumps(payload))], request=request)
    check = next(c for c in result.check_results if c.check_code == "CF-008")
    assert check.reason_code == "INSUFFICIENT_EVIDENCE" and not check.finding_local_ids
    assert result.status != "COMPLETED"
    assert result.check_results[0].finding_local_ids  # other valid check retained
    assert runtime.calls and len(runtime.calls) == 1
    assert result.attempt_diagnostics[0].normalized_output["check_results"][7]["findings"]


def test_frozen_payer_guard_runs_before_partial_absence():
    request = _request()
    request.perspective = "PARTY_B"
    request.our_party, request.counterparty = "乙方单位", "甲方单位"
    request.check_task_scopes = [CheckTaskScope(check_code="CF-005", expected_item_ids=["x", "y"], provided_item_ids=["x"])]
    _, ir, anchors, candidate = _prompt(request)
    candidate = candidate.model_copy(update={"payer_role_status": "COUNTERPARTY"})
    payload = _valid_payload()
    finding = copy.deepcopy(payload["check_results"][0]["findings"][0])
    finding.update(check_code="CF-005", risk_type="ADVANCE_PAYMENT_SECURITY_RISK",
        evidence=[{"evidence_type": "ABSENCE", "checked_scope": "全文", "verification_note": "未见保障"}])
    payload["check_results"][4].update(candidate_decision="RISK_CONFIRMED", findings=[finding])
    parsed = _parse_model_output(json.dumps(payload), ir_refs=ir, anchor_refs=anchors, cf005_candidate=candidate)
    enriched, _ = _enrich_reason_codes(parsed.response)
    coverage, findings, warnings = _materialize(request, enriched, ir, anchors, candidate)
    assert "RISK_PERSPECTIVE_CONFLICT" in warnings
    assert not any(f.check_code == "CF-005" for f in findings)


def test_one_check_uses_joint_facts_instead_of_independent_whole_contract_verdicts():
    planner = RiskReviewPlanBuilder()
    value = risk_plan_input()
    plan = planner.build(value)
    context = next(c for c in plan.contexts if c.projected_ir_items)
    projected = tuple(context.definitions + context.projected_ir_items)
    sources = tuple(context.source_excerpts)
    # Simulates the old 6000-token packing boundary without constructing a huge fixture.
    slices = planner._context_slices(value=value, checks=(context.check_specs[0],),
        projected=projected, excerpts=sources, candidates=(), hard_limit=1)
    assert len(slices) == 1
    assert slices[0].projected == projected and slices[0].excerpts == sources


@pytest.mark.asyncio
async def test_fenced_recovery_ignores_legacy_redis_lock_and_blocks_duplicate_owner(monkeypatch):
    from task_manager.runtime import execution
    from task_manager.runtime.fencing import ExecutionLease, bind_execution_lease
    checked = []
    async def verify(run_id):
        checked.append(run_id)
    monkeypatch.setattr(execution, "verify_current_execution_lease", verify)
    monkeypatch.setenv("TASK_EXECUTOR_LOCK_BACKEND", "redis")
    # No Redis server is running. A valid fenced recovery does not contact it.
    with bind_execution_lease(ExecutionLease(run_id="offline-run", owner="new", version=2)):
        async with execution.executor_lock("offline-run") as acquired:
            assert acquired
            async with execution.executor_lock("offline-run") as duplicate:
                assert not duplicate
        async with execution.executor_lock("offline-run") as released:
            assert released
    assert len(checked) == 3


@pytest.mark.asyncio
async def test_expired_lease_cannot_execute_even_before_a_new_owner_claims():
    from task_manager.models import utc_now
    from task_manager.runtime.fencing import ExecutionLease, RunLeaseLost, bind_execution_lease, verify_execution_lease
    run = NS(status="running", lease_owner="old", lease_version=1, lease_until=utc_now() - timedelta(seconds=1))
    with bind_execution_lease(ExecutionLease(run_id="r", owner="old", version=1)):
        with pytest.raises(RunLeaseLost):
            await verify_execution_lease(None, "r", run=run)


@pytest.mark.asyncio
async def test_busy_run_does_not_report_executed_or_spin(tmp_path, monkeypatch):
    from db.db_context import init_db, reset_engine_for_test
    from scheduling.scheduler import SchedulingRuntimeOptions
    from task_manager.runtime.worker import TaskWorker
    from task_manager.schemas import TaskCreateRequest, TaskRunRequest
    from task_manager.service import TaskManagerService
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'busy.db'}")
    reset_engine_for_test()
    await init_db()
    options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    service = TaskManagerService(options)
    task = await service.create_task(TaskCreateRequest(task_type="pipeline.demo", input_payload={"goal": "offline"},
        user_id="u", tenant_id="t"), service_name="ai-contract")
    await service.start_task_run(task.id, TaskRunRequest())
    async def busy(*args, **kwargs):
        return False
    monkeypatch.setattr(TaskManagerService, "_drain_prepared_task", busy)
    assert await TaskWorker(options, worker_id="test").run_once() is False
