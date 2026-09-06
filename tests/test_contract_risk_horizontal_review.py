from __future__ import annotations

import asyncio
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

from contract.application.idempotency import canonical_json
from contract.risk.plan_builder import RiskReviewPlanBuilder
from risk_fixture_loader import load_fixed_risk_plan_input
from service.conversation.llm_runner import LlmCompletionResult

from services.contract.capabilities.horizontal_review import (
    HORIZONTAL_CHECK_CODES,
    HorizontalBatch,
    HorizontalCandidate,
    HorizontalEvidenceSource,
    HorizontalReviewError,
    _batch_prompt,
    build_extended_bundle,
    build_horizontal_plan,
    build_relationship_index,
    execute_horizontal_phase,
    execute_horizontal_unit,
)
from services.contract.capabilities.risk_review_bundle import (
    BaseRiskReviewBundle,
    _generic_prompt,
    generic_input_diagnostics,
    generic_request_from_context,
)

FIXTURE_ENV = "CONTRACT_RISK_FIXTURE_DIR"
BASE_ARTIFACT_ENV = "CONTRACT_RISK_STAGE63_BUNDLE_ARTIFACT"


def _fixture():
    path = os.getenv(FIXTURE_ENV)
    if not path:
        pytest.skip(f"{FIXTURE_ENV} is not configured")
    return load_fixed_risk_plan_input(Path(path))


def _base_bundle() -> BaseRiskReviewBundle:
    path = os.getenv(BASE_ARTIFACT_ENV)
    if not path:
        pytest.skip(f"{BASE_ARTIFACT_ENV} is not configured")
    payload = json.loads(Path(path).read_text("utf-8"))
    return BaseRiskReviewBundle.model_validate(payload["raw_bundles"][0])


class _RiskRuntime:
    def __init__(self, _tenant_id: str, *, prompt_tokens: int = 3500) -> None:
        self.prompt_tokens = prompt_tokens

    async def complete_with_usage(self, **kwargs):
        prompt = json.loads(kwargs["messages"][0]["content"])
        decisions = []
        for candidate in prompt["candidate_decisions_required"]:
            decisions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "verdict": "RISK",
                    "decision_summary": "候选两侧或Trigger与Absence共同证明实质横向风险。",
                    "resolution_reason": None,
                    "severity_factors": candidate["allowed_severity_factors"][:1],
                    "supporting_evidence_source_ids": [],
                    "counter_evidence_source_ids": [],
                    "recommended_control_codes": candidate["allowed_control_codes"][:1],
                }
            )
        content = json.dumps(
            {"candidate_decisions": decisions},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return LlmCompletionResult(
            content=content,
            prompt_tokens=self.prompt_tokens,
            cached_tokens=100,
            completion_tokens=200,
            total_tokens=self.prompt_tokens + 200,
            time_to_first_token_ms=100,
            model_duration_ms=1200,
            trace_id="horizontal-test-trace",
            provider_request_id="horizontal-test-request",
            finish_reason="stop",
            review_unit_id=kwargs["review_unit_id"],
            review_id=kwargs["review_id"],
            framework_run_id=kwargs["framework_run_id"],
            attempt_no=kwargs["attempt_no"],
            repair_no=kwargs["repair_no"],
        )


def _runtime_factory(prompt_tokens: int = 3500):
    return lambda tenant_id: _RiskRuntime(tenant_id, prompt_tokens=prompt_tokens)


class _FailingRiskRuntime(_RiskRuntime):
    async def complete_with_usage(self, **kwargs):
        raise RuntimeError("injected horizontal Batch failure")


class _SlowRiskRuntime(_RiskRuntime):
    async def complete_with_usage(self, **kwargs):
        await asyncio.sleep(0.05)
        return await super().complete_with_usage(**kwargs)


def test_empty_legal_evidence_keeps_horizontal_prompt_on_legacy_shape() -> None:
    source_id = "horizontal-es-" + "1" * 32
    candidate_id = "horizontal-candidate-" + "2" * 32
    source = HorizontalEvidenceSource.model_construct(
        source_id=source_id,
        generation_id="generation-1",
        ir_id="I001",
        anchor_id="A001",
        block_id="block-1",
        block_no=1,
        heading_path=[],
        char_start=0,
        char_end=2,
        quoted_text="原文",
        quoted_text_hash="sha256:" + "3" * 64,
    )
    candidate = HorizontalCandidate.model_construct(
        candidate_id=candidate_id,
        unit_id="cross_clause_consistency",
        check_code="CCC-001",
        candidate_type="TERM_CONFLICT",
        candidate_strength="HARD_RULE",
        normalized_topic="期限",
        conflict_dimension=None,
        left_evidence_source_ids=[source_id],
        right_evidence_source_ids=[source_id],
        context_evidence_source_ids=[],
        absence_evidence_source_ids=[],
        left_normalized_claim="30天",
        right_normalized_claim="60天",
        possible_resolution_rules=[],
        allowed_counter_evidence_source_ids=[source_id],
        allowed_supporting_evidence_source_ids=[source_id],
        required_trigger_conditions=["期限冲突"],
        disqualifying_conditions=[],
        primary_evidence_requirements=["两处合同原文"],
        absence_evidence_requirements=[],
        canonical_root_type="TERM_CONFLICT",
        severity_rule_id="TERM_CONFLICT_V1",
        deterministic_severity_factors=[],
        allowed_severity_factors=[],
        allowed_control_codes=["ALIGN_TERM"],
        owner_type="HORIZONTAL",
        linked_base_finding_ids=[],
        requires_model_decision=True,
    )
    batch = HorizontalBatch(
        batch_id="risk-batch-" + "4" * 32,
        unit_id="cross_clause_consistency",
        check_codes=["CCC-001"],
        candidate_ids=[candidate_id],
        evidence_source_ids=[source_id],
        estimated_business_context_tokens=10,
    )
    plan = SimpleNamespace(
        candidates=[candidate],
        evidence_sources=[source],
        absence_evidence_sources=[],
    )

    prompt = _batch_prompt(plan, batch, legal_evidence=[])
    expected = {
        "task": "HORIZONTAL_CANDIDATE_DECISION",
        "unit_id": batch.unit_id,
        "candidate_decisions_required": [candidate.model_dump(mode="json")],
        "evidence_sources": [source.model_dump(mode="json")],
        "absence_evidence_sources": [],
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
            ]
        },
    }
    assert prompt == canonical_json(expected)
    assert "legal_evidence" not in prompt


def test_registry_and_horizontal_plan_cover_exactly_11_of_45_checks() -> None:
    plan = build_horizontal_plan(_fixture(), _base_bundle())
    assert tuple(plan.check_codes) == HORIZONTAL_CHECK_CODES
    assert len(plan.check_codes) == 11
    assert len(set(plan.check_codes)) == 11


def test_relationship_index_is_stable_and_avoids_full_pair_projection() -> None:
    value = _fixture()
    first, first_sources = build_relationship_index(value)
    second, second_sources = build_relationship_index(value)
    assert first == second
    assert first_sources == second_sources
    assert first.raw_pair_count > first.indexed_pair_count
    assert first.discarded_pair_count == first.raw_pair_count - first.indexed_pair_count
    assert first.indexed_pair_count < first.raw_pair_count
    assert first.index_hash == second.index_hash


def _fva_request():
    plan = RiskReviewPlanBuilder().build(_fixture())
    context = next(
        item
        for item in plan.contexts
        if str(getattr(item.unit_id, "value", item.unit_id))
        == "formation_validity_authority"
    )
    return plan, generic_request_from_context(context)


def test_fva002_input_physically_excludes_non_authority_sources() -> None:
    _plan, request = _fva_request()
    forbidden = (
        "人员专业资质",
        "项目人员",
        "上岗资格",
        "劳动合同",
        "社会保险",
        "社保",
        "履约团队",
        "技术能力",
        "项目经验",
        "履约能力",
        "服务能力",
    )
    assert not any(
        word in source.quoted_text
        for source in request.evidence_sources
        for word in forbidden
    )
    policy = next(
        item
        for item in request.check_evidence_policies
        if item.check_code == "FVA-002"
    )
    known = {item.source_id: item for item in request.evidence_sources}
    assert all(source_id in known for source_id in policy.allowed_evidence_source_ids)
    prompt, _ir_refs, _anchor_refs = _generic_prompt(request)
    payload = json.loads(prompt.split("\n", 1)[1])
    fva002 = next(
        item
        for item in payload["assigned_checks"]
        if item["check_code"] == "FVA-002"
    )
    assert {
        item[0] for item in fva002["allowed_evidence_sources"]
    } == set(policy.allowed_evidence_source_ids)
    assert "allowed_projected_ir" not in fva002
    assert "source_policy" in fva002
    assert "projected_ir" not in payload
    assert "source_excerpts" not in payload


@pytest.mark.asyncio
async def test_seven_batch_concurrent_build_is_stable_and_not_mutated_by_horizontal() -> None:
    value = _fixture()

    async def build_once():
        return await asyncio.to_thread(RiskReviewPlanBuilder().build, value)

    plans = await asyncio.gather(*(build_once() for _ in range(100)))
    plan_ids = {item.plan_id for item in plans}
    plan_hashes = {item.plan_hash for item in plans}
    assert len(plan_ids) == 1
    assert len(plan_hashes) == 1
    diagnostics = []
    for plan in plans:
        context = next(
            item
            for item in plan.contexts
            if str(getattr(item.unit_id, "value", item.unit_id))
            == "formation_validity_authority"
        )
        request = generic_request_from_context(context)
        diagnostics.append(generic_input_diagnostics(request))
    assert len(
        {
            (
                item["batch_context_hash"],
                item["assigned_checks_hash"],
                item["candidate_set_hash"],
                item["evidence_policy_hash"],
                item["serialized_prompt_hash"],
            )
            for item in diagnostics
        }
    ) == 1
    base_plan = plans[0]
    before = base_plan.model_dump(mode="json")
    build_horizontal_plan(value, _base_bundle())
    assert base_plan.model_dump(mode="json") == before
    assert all(plans[index] is not plans[index + 1] for index in range(99))


def test_fixed_fixture_horizontal_oracle_and_ownership() -> None:
    plan = build_horizontal_plan(_fixture(), _base_bundle())
    candidates = {item.candidate_type: item for item in plan.candidates}
    assert {
        "DATE_CHRONOLOGY_CONFLICT",
        "PARTY_NAME_CONFLICT",
        "MISSING_REFERENCED_ATTACHMENT",
    }.issubset(candidates)
    party = candidates["PARTY_NAME_CONFLICT"]
    assert set(party.left_evidence_source_ids).isdisjoint(
        party.right_evidence_source_ids
    )
    assert candidates["DATE_CHRONOLOGY_CONFLICT"].owner_type == "HORIZONTAL"
    assert candidates["PARTY_NAME_CONFLICT"].owner_type == "HORIZONTAL"
    missing = candidates["MISSING_REFERENCED_ATTACHMENT"]
    assert missing.owner_type == "HORIZONTAL"
    assert missing.context_evidence_source_ids
    assert missing.absence_evidence_source_ids
    absence = {
        item.source_id: item for item in plan.absence_evidence_sources
    }[missing.absence_evidence_source_ids[0]]
    assert absence.trigger_evidence_source_ids == missing.context_evidence_source_ids
    base_owned = [item for item in plan.candidates if item.owner_type == "BASE_DOMAIN"]
    assert base_owned
    assert all(item.linked_base_finding_ids for item in base_owned)
    assert all(not item.requires_model_decision for item in base_owned)


def test_consistency_candidates_have_two_grounded_sides() -> None:
    plan = build_horizontal_plan(_fixture(), _base_bundle())
    source_ids = {item.source_id for item in plan.evidence_sources}
    for candidate in plan.candidates:
        if candidate.unit_id != "cross_clause_consistency":
            continue
        assert candidate.left_evidence_source_ids
        assert candidate.right_evidence_source_ids
        assert set(candidate.left_evidence_source_ids).issubset(source_ids)
        assert set(candidate.right_evidence_source_ids).issubset(source_ids)


def test_plan_hash_candidate_ids_and_batch_partition_are_stable_100_times() -> None:
    value = _fixture()
    base = _base_bundle()
    first = build_horizontal_plan(value, base)
    for _ in range(100):
        current = build_horizontal_plan(value, base)
        assert current.plan_id == first.plan_id
        assert current.plan_hash == first.plan_hash
        assert current.candidates == first.candidates
        assert current.batches == first.batches
    assert len(first.batches) == 3
    assert sum(
        item.unit_id == "cross_clause_consistency" for item in first.batches
    ) == 2
    assert sum(
        item.unit_id == "missing_ambiguity_completeness" for item in first.batches
    ) == 1
    assert all(item.estimated_business_context_tokens <= 5000 for item in first.batches)
    assert all(len(item.evidence_source_ids) < 101 for item in first.batches)


@pytest.mark.asyncio
async def test_consistency_five_replays_are_stable_and_grounded() -> None:
    value = _fixture()
    plan = build_horizontal_plan(value, _base_bundle())
    results = [
        await execute_horizontal_unit(
            value,
            plan,
            "cross_clause_consistency",
            tenant_id="0",
            model_id="test-model",
            runtime_factory=_runtime_factory(),
        )
        for _ in range(5)
    ]
    snapshots = [
        [
            (
                item.check_code,
                item.root_type,
                item.risk_level,
                tuple(item.primary_evidence_source_ids),
            )
            for item in result.canonical_roots
        ]
        for result in results
    ]
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert all(result.model_call_count == 2 for result in results)
    assert all(result.repair_count == 0 and result.tool_call_count == 0 for result in results)
    assert all(len(result.check_results) == 5 for result in results)
    assert all(len(result.findings) == 2 for result in results)
    assert all(
        evidence.quoted_text
        for result in results
        for finding in result.findings
        for evidence in finding.evidence_candidates
        if evidence.evidence_type == "TEXT_QUOTE"
    )


@pytest.mark.asyncio
async def test_completeness_five_replays_keep_absence_and_suppress_base_duplicates() -> None:
    value = _fixture()
    plan = build_horizontal_plan(value, _base_bundle())
    results = [
        await execute_horizontal_unit(
            value,
            plan,
            "missing_ambiguity_completeness",
            tenant_id="0",
            model_id="test-model",
            runtime_factory=_runtime_factory(),
        )
        for _ in range(5)
    ]
    assert all(result.model_call_count == 1 for result in results)
    assert all(len(result.check_results) == 6 for result in results)
    assert all(len(result.findings) == 1 for result in results)
    assert all(
        any(
            evidence.evidence_type == "ABSENCE"
            for evidence in result.findings[0].evidence_candidates
        )
        for result in results
    )
    assert all(
        any(item.reason_code == "CONFIRMED_BY_BASE_DOMAIN" for item in result.check_results)
        for result in results
    )


@pytest.mark.asyncio
async def test_large_provider_prompt_preserves_horizontal_materialization() -> None:
    value = _fixture()
    plan = build_horizontal_plan(value, _base_bundle())
    result = await execute_horizontal_unit(
        value,
        plan,
        "missing_ambiguity_completeness",
        tenant_id="0",
        model_id="test-model",
        runtime_factory=_runtime_factory(17694),
    )
    assert result.status == "COMPLETED"
    assert result.findings
    assert result.model_call_count == 1
    assert result.batch_metrics[0].prompt_budget.budget_status == "SOFT_WARNING"


@pytest.mark.asyncio
async def test_extended_bundle_contains_all_45_checks_and_all_findings() -> None:
    value = _fixture()
    base = _base_bundle()
    plan = build_horizontal_plan(value, base)
    units, peak = await execute_horizontal_phase(
        value,
        plan,
        tenant_id="0",
        model_id="test-model",
        runtime_factory=_runtime_factory(),
    )
    extended = build_extended_bundle(
        value=value,
        base_bundle=base,
        horizontal_plan=plan,
        horizontal_units=units,
        base_phase_wall_ms=100,
        horizontal_candidate_build_ms=10,
        horizontal_phase_wall_ms=20,
        horizontal_peak_concurrency=peak,
    )
    assert len(extended.check_codes) == 45
    assert extended.metrics.horizontal_peak_concurrency == 2
    assert extended.metrics.model_call_count == 10
    assert len(extended.findings) == (
        sum(len(unit.findings) for unit in base.units)
        + sum(len(unit.findings) for unit in units)
    )


@pytest.mark.asyncio
async def test_extended_bundle_rejects_stale_generation_without_partial_result() -> None:
    value = _fixture()
    base = _base_bundle()
    plan = build_horizontal_plan(value, base)
    units, peak = await execute_horizontal_phase(
        value,
        plan,
        tenant_id="0",
        model_id="test-model",
        runtime_factory=_runtime_factory(),
    )
    stale = value.model_copy(update={"generation_id": "stale-generation"})
    with pytest.raises(HorizontalReviewError) as error:
        build_extended_bundle(
            value=stale,
            base_bundle=base,
            horizontal_plan=plan,
            horizontal_units=units,
            base_phase_wall_ms=100,
            horizontal_candidate_build_ms=10,
            horizontal_phase_wall_ms=20,
            horizontal_peak_concurrency=peak,
        )
    assert error.value.code == "EXTENDED_IDENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_consistency_batch_failure_is_scoped_to_horizontal_unit() -> None:
    value = _fixture()
    plan = build_horizontal_plan(value, _base_bundle())
    result = await execute_horizontal_unit(
        value,
        plan,
        "cross_clause_consistency",
        tenant_id="0",
        model_id="test-model",
        runtime_factory=lambda tenant_id: _FailingRiskRuntime(tenant_id),
    )
    assert result.status in {"PARTIAL_FAILED", "FAILED"}
    assert all(item.status == "FAILED" for item in result.batch_metrics)
    assert any(
        item.verdict == "INSUFFICIENT_EVIDENCE"
        for item in result.decisions
    )
    assert result.findings == []


@pytest.mark.asyncio
async def test_completeness_batch_timeout_is_scoped_to_horizontal_unit() -> None:
    value = _fixture()
    plan = build_horizontal_plan(value, _base_bundle())
    result = await execute_horizontal_unit(
        value,
        plan,
        "missing_ambiguity_completeness",
        tenant_id="0",
        model_id="test-model",
        runtime_factory=lambda tenant_id: _SlowRiskRuntime(tenant_id),
        timeout_seconds=0.001,
    )
    assert result.status in {"PARTIAL_FAILED", "FAILED"}
    assert all(item.status == "FAILED" for item in result.batch_metrics)
    assert all(
        item.verdict == "INSUFFICIENT_EVIDENCE"
        for item in result.decisions
        if item.owner_type != "BASE_DOMAIN"
    )


@pytest.mark.asyncio
async def test_extended_bundle_rejects_ownership_conflict_without_partial_result() -> None:
    value = _fixture()
    base = _base_bundle()
    plan = build_horizontal_plan(value, base)
    units, peak = await execute_horizontal_phase(
        value,
        plan,
        tenant_id="0",
        model_id="test-model",
        runtime_factory=_runtime_factory(),
    )
    base_finding = next(
        finding for unit in base.units for finding in unit.findings
    )
    horizontal_finding = units[0].findings[0]
    conflicting = horizontal_finding.model_copy(
        update={
            "finding_local_id": base_finding.finding_local_id,
            "check_code": base_finding.check_code,
            "risk_type": base_finding.risk_type,
        }
    )
    units[0] = units[0].model_copy(
        update={"findings": [conflicting, *units[0].findings[1:]]}
    )
    with pytest.raises(HorizontalReviewError) as error:
        build_extended_bundle(
            value=value,
            base_bundle=base,
            horizontal_plan=plan,
            horizontal_units=units,
            base_phase_wall_ms=100,
            horizontal_candidate_build_ms=10,
            horizontal_phase_wall_ms=20,
            horizontal_peak_concurrency=peak,
        )
    assert error.value.code == "EXTENDED_FINDING_OWNERSHIP_CONFLICT"


@pytest.mark.asyncio
async def test_extended_bundle_rejects_missing_check_without_partial_result() -> None:
    value = _fixture()
    base = _base_bundle()
    plan = build_horizontal_plan(value, base)
    units, peak = await execute_horizontal_phase(
        value,
        plan,
        tenant_id="0",
        model_id="test-model",
        runtime_factory=_runtime_factory(),
    )
    incomplete = plan.model_copy(update={"check_codes": plan.check_codes[:-1]})
    with pytest.raises(HorizontalReviewError) as error:
        build_extended_bundle(
            value=value,
            base_bundle=base,
            horizontal_plan=incomplete,
            horizontal_units=units,
            base_phase_wall_ms=100,
            horizontal_candidate_build_ms=10,
            horizontal_phase_wall_ms=20,
            horizontal_peak_concurrency=peak,
        )
    assert error.value.code == "EXTENDED_CHECK_COVERAGE_INVALID"


@pytest.mark.asyncio
async def test_extended_bundle_rejects_cross_generation_evidence() -> None:
    value = _fixture()
    base = _base_bundle()
    plan = build_horizontal_plan(value, base)
    units, peak = await execute_horizontal_phase(
        value,
        plan,
        tenant_id="0",
        model_id="test-model",
        runtime_factory=_runtime_factory(),
    )
    evidence = plan.evidence_sources[0].model_copy(
        update={"generation_id": "stale-generation"}
    )
    stale = plan.model_copy(
        update={"evidence_sources": [evidence, *plan.evidence_sources[1:]]}
    )
    with pytest.raises(HorizontalReviewError) as error:
        build_extended_bundle(
            value=value,
            base_bundle=base,
            horizontal_plan=stale,
            horizontal_units=units,
            base_phase_wall_ms=100,
            horizontal_candidate_build_ms=10,
            horizontal_phase_wall_ms=20,
            horizontal_peak_concurrency=peak,
        )
    assert error.value.code == "EXTENDED_HORIZONTAL_EVIDENCE_STALE"


def test_base_artifact_is_not_modified_by_horizontal_plan_building() -> None:
    value = _fixture()
    base = _base_bundle()
    before = base.model_dump(mode="json")
    build_horizontal_plan(value, base)
    assert base.model_dump(mode="json") == before
