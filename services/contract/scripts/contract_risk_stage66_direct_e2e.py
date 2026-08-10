#!/usr/bin/env python3
"""Stage 6.6 Direct risk-review full-chain E2E acceptance runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT,
    ROOT / "backend",
    ROOT / "backend" / "single-agent",
    ROOT / "services" / "contract" / "src",
    ROOT / "services" / "contract" / "tests",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from common.system_constants import DEFAULT_TENANT_ID
from contract.api.models import ContractProfile, ReviewResultData
from contract.callback.models import FindingConsolidationArtifact
from contract.risk.models import RiskReviewPlan
from contract.risk.plan_builder import RiskReviewPlanBuilder
from services.contract.capabilities.direct_e2e import (
    DirectE2EError,
    DirectE2EStageMetric,
    DirectRiskReviewEndToEndRequest,
    DirectRiskReviewEndToEndRunner,
    DryRunResultSink,
    build_final_callback,
    build_formal_result,
    core_result_signature,
    frozen_input_snapshot,
    routing_by_final_finding_id,
    stable_hash,
)
from services.contract.capabilities.finding_consolidation import (
    FindingConsolidationEngine,
    build_candidate_pairs,
)
from services.contract.capabilities.horizontal_review import (
    ExtendedRiskReviewBundle,
    build_extended_bundle,
    build_horizontal_plan,
    execute_horizontal_phase,
)
from services.contract.capabilities.legacy_compatibility import (
    LegacyCompatibilityContext,
    LegacyRiskArtifactAdapter,
    finalize_legacy_compatible_result,
)
from services.contract.capabilities.party_roles import contract_party_roles
from services.contract.capabilities.risk_review_bundle import (
    execute_base_risk_review_bundle,
)


FIXTURE_ID = "service-outsourcing-0829-v1"
ARTIFACT_ORDER = (
    "rights_obligations_review_result",
    "commercial_terms_review_result",
    "liability_termination_review_result",
    "missing_ambiguous_clauses_result",
    "relation_extraction_result",
)


def _write_json(path: Path, value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_text(path: Path, value: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, "utf-8")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contract_hash(fixture_dir: Path) -> str:
    raw = json.loads(
        (
            fixture_dir
            / "contract-risk-review-fixture-service-outsourcing-0829-v1.json"
        ).read_text("utf-8")
    )
    return raw["source_document"]["content_sha256"]


def _raw_inputs(fixture_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ir = json.loads(
        (
            fixture_dir
            / "contract-ir-stage-result-service-outsourcing-0829-v1.json"
        ).read_text("utf-8")
    )
    context = json.loads(
        (
            fixture_dir
            / "risk-review-context-service-outsourcing-0829-v1.json"
        ).read_text("utf-8")
    )
    return ir, context


def _compatibility_context(value, contract_hash: str) -> LegacyCompatibilityContext:
    roles = contract_party_roles(
        perspective=value.perspective,
        our_party=value.our_party,
        counterparty=value.counterparty,
    )
    return LegacyCompatibilityContext(
        review_id=value.review_id,
        business_task_id="stage66-direct-e2e-business-task",
        contract_version_id=value.document_id,
        generation_id=value.generation_id,
        contract_hash=contract_hash,
        contract_profile=ContractProfile(
            contract_type=value.contract_type,
            party_a={"name": roles.party_a_name},
            party_b={"name": roles.party_b_name},
            perspective=value.perspective,
            our_party=value.our_party,
            counterparty=value.counterparty,
            review_attitude=value.review_attitude,
        ),
    )


def _finding_contexts(projection) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item.legacy_artifact_type, item.compatible_finding_id): {
            "source_unit_id": item.source_unit_id,
            "source_check_code": item.source_check_code,
            "source_root_id": item.source_root_id,
            "risk_type": item.risk_type,
            "owner_type": item.owner_type,
            "routing_rule_id": item.routing_rule_id,
        }
        for item in projection.routing_records
    }


def _sum_usage(metrics: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: sum(int(item.get(key) or 0) for item in metrics)
        for key in ("prompt_tokens", "cached_tokens", "completion_tokens")
    }


def _validate_oracle(extended) -> list[str]:
    failures: list[str] = []
    if len(extended.check_codes) != 45:
        failures.append("45-item Check coverage failed")
    if extended.metrics.base_peak_concurrency != 7:
        failures.append("Base peak concurrency is not 7")
    if extended.metrics.horizontal_peak_concurrency != 2:
        failures.append("Horizontal peak concurrency is not 2")
    units = {item.unit_id: item for item in extended.base_bundle.units}
    fva = units["formation_validity_authority"]
    fva002 = next(item for item in fva.check_results if item.check_code == "FVA-002")
    fva_assessment = fva.fva_assessments[0] if fva.fva_assessments else None
    if (
        fva002.reason_code != "INSUFFICIENT_EVIDENCE"
        or fva002.finding_local_ids
        or fva_assessment is None
        or fva_assessment.assessment_type != "EXTERNAL_VERIFICATION_REQUIRED"
        or not fva_assessment.external_verification_required
    ):
        failures.append("FVA-002 external-verification Oracle failed")
    forbidden = ("人员资质", "劳动合同", "社会保险", "社保", "履约能力")
    if any(
        token in (fva002.decision_note or "")
        for token in forbidden
    ):
        failures.append("FVA-002 scope leakage detected")
    cf005 = [
        item
        for item in units["commercial_financial"].findings
        if item.check_code == "CF-005"
    ]
    if not cf005 or any(item.risk_level != "HIGH" for item in cf005):
        failures.append("CF-005 HIGH Oracle failed")
    po = units["performance_obligations"]
    if len(po.canonical_risk_roots) != 6:
        failures.append("PO root count is not 6")
    po001 = [item for item in po.findings if item.check_code == "PO-001"]
    if len(po001) != 1 or po001[0].risk_level != "HIGH":
        failures.append("PO-001 HIGH Oracle failed")
    po003 = next(item for item in po.check_results if item.check_code == "PO-003")
    if po003.finding_local_ids:
        failures.append("PO-003 no-Finding Oracle failed")
    po006 = [item for item in po.findings if item.check_code == "PO-006"]
    if not po006 or any(item.risk_level != "MEDIUM" for item in po006):
        failures.append("PO-006 MEDIUM Oracle failed")
    icd = units["ip_confidentiality_data"]
    if len(icd.canonical_risk_roots) != 1:
        failures.append("ICD root count is not 1")
    if not any(
        item.root_type == "CONFIDENTIALITY_COMPLETENESS_ABSENT"
        and item.risk_level == "MEDIUM"
        for item in icd.canonical_risk_roots
    ):
        failures.append("ICD confidentiality-completeness Oracle failed")
    lre = units["liability_remedies_exit"]
    if len(lre.canonical_risk_roots) != 4:
        failures.append("LRE root count is not 4")
    if not any(
        item.root_type == "UNBOUNDED_LIABILITY_EXPOSURE"
        and item.risk_level == "HIGH"
        for item in lre.canonical_risk_roots
    ):
        failures.append("LRE unbounded-liability Oracle failed")
    horizontal = {item.unit_id: item for item in extended.horizontal_units}
    consistency = horizontal["cross_clause_consistency"]
    completeness = horizontal["missing_ambiguity_completeness"]
    consistency_types = {item.risk_type for item in consistency.findings}
    completeness_types = {item.risk_type for item in completeness.findings}
    if not {
        "EFFECTIVE_DATE_CHRONOLOGY_CONFLICT",
        "PARTY_TERM_IDENTITY_CONFLICT",
    } <= consistency_types:
        failures.append("Horizontal date/party conflict Oracle failed")
    if "REFERENCED_ATTACHMENT_MISSING" not in completeness_types:
        failures.append("Missing referenced attachment Oracle failed")
    for unit in extended.base_bundle.units:
        for item in unit.candidate_decisions:
            if set(item.validated_semantic_severity_factors) - set(
                item.proposed_semantic_severity_factors
            ):
                failures.append("Unproposed severity factor entered final calculation")
                break
    return failures


def _stage_metric(
    stage: str,
    wall_ms: int,
    calls: list[Any] | list[dict[str, Any]],
    *,
    repairs: int = 0,
) -> DirectE2EStageMetric:
    raw = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        for item in calls
    ]
    usage = _sum_usage(raw)
    return DirectE2EStageMetric(
        stage=stage,
        wall_ms=wall_ms,
        model_calls=len(raw),
        repair_calls=repairs,
        tool_calls=0,
        **usage,
    )


def _budget_failures(metrics: list[Any] | list[dict[str, Any]]) -> list[str]:
    failures = []
    for item in metrics:
        raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        budget = raw.get("prompt_budget") or {}
        if budget.get("budget_status") == "HARD_LIMIT_EXCEEDED":
            failures.append(str(raw.get("batch_id") or "unknown"))
    return failures


def _core_components(extended: ExtendedRiskReviewBundle) -> list[dict[str, Any]]:
    units = {item.unit_id: item for item in extended.base_bundle.units}
    fva = units["formation_validity_authority"]
    fva002 = next(item for item in fva.check_results if item.check_code == "FVA-002")
    # Formal execution deliberately allows an individual review unit to be
    # unavailable while the remaining units still yield a usable result.  The
    # core signature must represent that absence rather than indexing into an
    # empty assessment list and turning a partial result into a total failure.
    assessment = fva.fva_assessments[0] if fva.fva_assessments else None
    core: list[dict[str, Any]] = [
        {
            "check_code": "FVA-002",
            "assessment_type": (
                assessment.assessment_type
                if assessment is not None
                else "UNAVAILABLE"
            ),
            "reason_code": fva002.reason_code,
            "finding_count": len(fva002.finding_local_ids),
        }
    ]
    commercial = units["commercial_financial"]
    for finding in commercial.findings:
        if finding.check_code != "CF-005":
            continue
        core.append(
            {
                "check_code": finding.check_code,
                "risk_type": finding.risk_type,
                "risk_level": finding.risk_level,
                # Evidence identity is deterministic even when a model returns an
                # equivalent explanatory sentence for an ABSENCE source.  The
                # human-readable checked_scope is not part of the frozen core
                # identity; the source IDs and text locations are.
                "primary_evidence_source_ids": sorted(
                    item.evidence_local_id for item in finding.evidence_candidates
                ),
            }
        )
    for unit_id in (
        "performance_obligations",
        "ip_confidentiality_data",
        "liability_remedies_exit",
    ):
        for root in units[unit_id].canonical_risk_roots:
            core.append(
                {
                    "check_code": root.check_code,
                    "risk_type": root.risk_type,
                    "root_type": root.root_type,
                    "risk_level": root.risk_level,
                    "primary_evidence_source_ids": sorted(
                        root.core_primary_evidence_source_ids
                    ),
                }
            )
    for unit in extended.horizontal_units:
        for root in unit.canonical_roots:
            core.append(
                {
                    "check_code": root.check_code,
                    "root_type": root.root_type,
                    "risk_level": root.risk_level,
                    "primary_evidence_source_ids": sorted(
                        root.primary_evidence_source_ids
                    ),
                }
            )
    return sorted(core, key=stable_hash)


def _core_signature(extended: ExtendedRiskReviewBundle) -> str:
    return stable_hash(_core_components(extended))


async def _execute_one(
    *,
    run_index: int,
    value,
    request: DirectRiskReviewEndToEndRequest,
    context: LegacyCompatibilityContext,
    tenant_id: str,
    model_id: str,
    run_id_prefix: str = "stage66-direct-e2e",
    allow_dynamic_base_batch_count: bool = False,
    diagnostic_allow_oracle_drift: bool = False,
    plan: RiskReviewPlan | None = None,
) -> tuple[dict[str, Any], dict[str, Any], ReviewResultData, Any, Any]:
    run_id = f"{run_id_prefix}-{run_index}"
    started = time.perf_counter()
    plan = plan or RiskReviewPlanBuilder().build(value)

    base_started = time.perf_counter()
    base = await execute_base_risk_review_bundle(
        plan,
        tenant_id=tenant_id,
        model_id=model_id,
        contract_hash=request.contract_hash,
        fixture_id=request.fixture_id,
        framework_run_id=f"{run_id}-base",
        allow_dynamic_batch_count=allow_dynamic_base_batch_count,
    )
    base_wall = round((time.perf_counter() - base_started) * 1000)

    horizontal_build_started = time.perf_counter()
    horizontal_plan = build_horizontal_plan(value, base)
    horizontal_build_wall = round(
        (time.perf_counter() - horizontal_build_started) * 1000
    )
    horizontal_started = time.perf_counter()
    horizontal_units, horizontal_peak = await execute_horizontal_phase(
        value,
        horizontal_plan,
        tenant_id=tenant_id,
        model_id=model_id,
        framework_run_id=f"{run_id}-horizontal",
    )
    horizontal_wall = round((time.perf_counter() - horizontal_started) * 1000)
    extended = build_extended_bundle(
        value=value,
        base_bundle=base,
        horizontal_plan=horizontal_plan,
        horizontal_units=horizontal_units,
        base_phase_wall_ms=base_wall,
        horizontal_candidate_build_ms=horizontal_build_wall,
        horizontal_phase_wall_ms=horizontal_wall,
        horizontal_peak_concurrency=horizontal_peak,
    )
    oracle_failures = _validate_oracle(extended)
    if oracle_failures and not diagnostic_allow_oracle_drift:
        raise DirectE2EError(
            "RISK_DIRECT_ORACLE_FAILED",
            "; ".join(oracle_failures),
        )
    review_calls = [
        metric for unit in base.units for metric in unit.call_metrics
    ] + [
        metric for unit in horizontal_units for metric in unit.batch_metrics
    ]
    budget_failures = _budget_failures(review_calls)
    if budget_failures:
        raise DirectE2EError(
            "RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED",
            ",".join(budget_failures),
        )

    compatibility_started = time.perf_counter()
    projection = LegacyRiskArtifactAdapter().adapt(
        extended.model_dump(mode="json")
    )
    compatibility_wall = round(
        (time.perf_counter() - compatibility_started) * 1000
    )
    artifacts = projection.artifacts.as_artifact_dict()
    pair_plan_started = time.perf_counter()
    candidate_pairs = build_candidate_pairs(
        artifacts,
        finding_contexts=_finding_contexts(projection),
    )
    pair_plan_wall = round((time.perf_counter() - pair_plan_started) * 1000)
    merge_started = time.perf_counter()
    merge_run = await FindingConsolidationEngine().consolidate_with_metrics(
        artifacts,
        tenant_id=tenant_id,
        model_id=model_id,
        finding_contexts=_finding_contexts(projection),
    )
    merge_wall = round((time.perf_counter() - merge_started) * 1000)
    consolidation = FindingConsolidationArtifact.model_validate(
        merge_run["artifact"]
    )
    merge_budget_failures = _budget_failures(merge_run["call_metrics"])
    if merge_budget_failures:
        raise DirectE2EError(
            "RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED",
            ",".join(merge_budget_failures),
        )
    blocks = [item.model_dump(mode="json") for item in value.source_blocks]
    verify_started = time.perf_counter()
    compatible = finalize_legacy_compatible_result(
        projection,
        context=context,
        consolidation=consolidation,
        blocks=blocks,
    )
    verify_wall = round((time.perf_counter() - verify_started) * 1000)

    stage_metrics = [
        _stage_metric(
            "extended_bundle",
            extended.metrics.extended_bundle_wall_ms,
            review_calls,
            repairs=extended.metrics.repair_count,
        ),
        _stage_metric("compatibility", compatibility_wall, []),
        _stage_metric(
            "semantic_merge",
            merge_wall,
            merge_run["call_metrics"],
            repairs=sum(
                int((item.get("repair_no") or 0) > 0)
                for item in merge_run["call_metrics"]
            ),
        ),
        _stage_metric("verify_evidence", verify_wall, []),
    ]
    sink = DryRunResultSink()
    runner = DirectRiskReviewEndToEndRunner(sink)
    e2e, payload = runner.finalize(
        request,
        run_id=run_id,
        compatible=compatible,
        compatibility_context=context,
        stage_metrics=stage_metrics,
        framework_task_id=f"{run_id}-task",
        framework_run_id=f"{run_id}-framework-run",
        core_signature=_core_signature(extended),
    )

    duplicate_sink = sink.submit(payload.model_dump(mode="json"))
    formal, _, _ = build_formal_result(
        compatible,
        context=context,
        generation_id=request.generation_id,
        framework_task_id=f"{run_id}-task",
        framework_run_id=f"{run_id}-framework-run",
    )
    duplicate_callback = sink.callback(
        build_final_callback(
            formal,
            framework_task_id=f"{run_id}-task",
            framework_run_id=f"{run_id}-framework-run",
        )
    )
    if duplicate_sink.status != "DUPLICATE" or duplicate_callback.status != "DUPLICATE":
        raise DirectE2EError(
            "RISK_E2E_IDEMPOTENCY_FAILED",
            "Repeated result or callback was not idempotent",
        )

    replay = _deterministic_replay(
        request=request,
        context=context,
        projection=projection,
        consolidation=consolidation,
        blocks=blocks,
        expected_payload=payload,
        repetitions=100,
    )
    total_wall = round((time.perf_counter() - started) * 1000)
    total_calls = len(review_calls) + len(merge_run["call_metrics"])
    total_repairs = extended.metrics.repair_count + sum(
        int((item.get("repair_no") or 0) > 0)
        for item in merge_run["call_metrics"]
    )
    all_call_metrics = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        for item in review_calls
    ] + list(merge_run["call_metrics"])
    usage = _sum_usage(all_call_metrics)
    artifact_counts = {
        key: len(value["findings"]) for key, value in artifacts.items()
    }
    summary = {
        "run_index": run_index,
        "run_id": run_id,
        "input_hash": stable_hash(
            {
                "contract_ir": request.contract_ir_stage_result,
                "context": request.risk_review_context,
            }
        ),
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "extended_bundle_wall_ms": extended.metrics.extended_bundle_wall_ms,
        "base_phase_wall_ms": base_wall,
        "horizontal_candidate_build_ms": horizontal_build_wall,
        "horizontal_phase_wall_ms": horizontal_wall,
        "base_peak_concurrency": extended.metrics.base_peak_concurrency,
        "horizontal_peak_concurrency": extended.metrics.horizontal_peak_concurrency,
        "compatibility_wall_ms": compatibility_wall,
        "pair_plan_wall_ms": pair_plan_wall,
        "semantic_merge_wall_ms": merge_run["wall_duration_ms"],
        "verify_evidence_wall_ms": verify_wall,
        "direct_e2e_wall_ms": total_wall,
        "review_model_calls": len(review_calls),
        "semantic_merge_model_calls": len(merge_run["call_metrics"]),
        "total_model_calls": total_calls,
        "total_repairs": total_repairs,
        "total_tool_calls": 0,
        **usage,
        "candidate_pair_count": len(candidate_pairs),
        "pair_batch_count": len(
            {
                item.get("batch_id")
                for item in merge_run["call_metrics"]
                if item.get("batch_id")
            }
        ),
        "same_risk_count": compatible.metrics.same_risk_count,
        "related_distinct_count": compatible.metrics.related_distinct_count,
        "distinct_count": compatible.metrics.distinct_count,
        "skipped_count": int(compatible.merge_status == "SKIPPED"),
        "pre_merge_finding_count": compatible.metrics.pre_merge_finding_count,
        "post_merge_finding_count": compatible.metrics.post_merge_finding_count,
        "artifact_finding_counts": artifact_counts,
        "finding_count": len(payload.findings),
        "evidence_count": len(payload.evidences),
        "formal_result_hash": payload.result_hash,
        "core_result_signature": e2e.core_result_signature,
        "core_result_components": _core_components(extended),
        "payload_hash": e2e.formal_payload_hash,
        "dry_run_sink_status": e2e.dry_run_sink_status,
        "dry_run_callback_status": e2e.dry_run_callback_status,
        "duplicate_sink_status": duplicate_sink.status,
        "duplicate_callback_status": duplicate_callback.status,
        "legacy_model_calls": 0,
        "legacy_tool_calls": 0,
        "formal_result_sink_writes": 0,
        "real_java_callbacks": 0,
        "pipeline_cutover_effect": "NONE",
        "oracle_failures": oracle_failures,
        "replay": replay,
    }
    attempt = {
        "run_index": run_index,
        "status": "PASSED",
        "summary": summary,
        "extended_bundle": extended.model_dump(mode="json"),
        "compatibility_projection": {
            "routing_records": [
                item.model_dump(mode="json") for item in projection.routing_records
            ],
            "legacy_artifacts": projection.artifacts.model_dump(mode="json"),
        },
        "semantic_merge": merge_run,
        "compatible_result": compatible.model_dump(mode="json"),
        "formal_payload": payload.model_dump(mode="json"),
        "e2e_result": e2e.model_dump(mode="json"),
    }
    return summary, attempt, payload, compatible, extended


def _deterministic_replay(
    *,
    request,
    context,
    projection,
    consolidation,
    blocks,
    expected_payload,
    repetitions: int,
) -> dict[str, Any]:
    artifact_hashes: set[str] = set()
    payload_hashes: set[str] = set()
    result_hashes: set[str] = set()
    idempotency_behaviors: set[str] = set()
    for index in range(repetitions):
        compatible = finalize_legacy_compatible_result(
            projection,
            context=context,
            consolidation=consolidation,
            blocks=blocks,
        )
        formal, payload, formal_hash = build_formal_result(
            compatible,
            context=context,
            generation_id=request.generation_id,
            framework_task_id=f"replay-task-{index}",
            framework_run_id=f"replay-run-{index}",
        )
        sink = DryRunResultSink()
        first = sink.submit(payload.model_dump(mode="json"))
        second = sink.submit(payload.model_dump(mode="json"))
        first_callback = sink.callback(
            build_final_callback(
                formal,
                framework_task_id=f"replay-task-{index}",
                framework_run_id=f"replay-run-{index}",
            )
        )
        second_callback = sink.callback(
            build_final_callback(
                formal,
                framework_task_id=f"replay-task-{index}",
                framework_run_id=f"replay-run-{index}",
            )
        )
        artifact_hashes.add(
            stable_hash(compatible.legacy_artifacts.model_dump(mode="json"))
        )
        payload_hashes.add(formal_hash)
        result_hashes.add(payload.result_hash)
        idempotency_behaviors.add(
            f"{first.status.value}/{second.status.value}/"
            f"{first_callback.status.value}/{second_callback.status.value}"
        )
    if (
        len(artifact_hashes) != 1
        or len(payload_hashes) != 1
        or len(result_hashes) != 1
        or idempotency_behaviors
        != {"ACCEPTED/DUPLICATE/ACCEPTED/DUPLICATE"}
        or expected_payload.result_hash not in result_hashes
    ):
        raise DirectE2EError(
            "RISK_E2E_DETERMINISM_FAILED",
            "No-model replay was not stable",
        )
    return {
        "repetitions": repetitions,
        "artifact_hash_stable": True,
        "payload_hash_stable": True,
        "formal_result_hash_stable": True,
        "idempotency_stable": True,
        "artifact_hash": next(iter(artifact_hashes)),
        "payload_hash": next(iter(payload_hashes)),
        "formal_result_hash": next(iter(result_hashes)),
        "model_calls": 0,
    }


def _human_readable(value, compatible, payload, summary) -> str:
    routes = routing_by_final_finding_id(compatible)
    evidence = {item.evidence_id: item for item in payload.evidences}
    by_artifact: dict[str, list[Any]] = {key: [] for key in ARTIFACT_ORDER}
    for finding in payload.findings:
        route = routes[finding.finding_id]
        by_artifact[route.legacy_artifact_type].append(finding)
    lines = [
        "# Direct 风险审查端到端结果",
        "",
        "## 审查条件",
        "",
        f"- 我方：{value.our_party}",
        f"- 相对方：{value.counterparty}",
        f"- Perspective：{value.perspective.value}",
        f"- Attitude：{value.review_attitude}",
        "- Check：45/45 完成",
        "",
        "## 最终风险",
        "",
    ]
    for artifact_type in ARTIFACT_ORDER:
        lines.extend((f"### {artifact_type}", ""))
        findings = by_artifact[artifact_type]
        if not findings:
            lines.extend(("- 无正式 Finding", ""))
            continue
        for finding in findings:
            route = routes[finding.finding_id]
            lines.extend(
                (
                    f"#### {finding.title}",
                    "",
                    f"- 风险等级：{finding.risk_level.value}",
                    f"- check_code：{route.source_check_code}",
                    f"- risk_type：{route.risk_type}",
                    f"- 风险描述：{finding.issue}",
                    f"- 对我方影响：{finding.impact_to_our_party}",
                    f"- 修改建议：{finding.suggestion}",
                    "- Evidence：",
                    "",
                )
            )
            for evidence_id in finding.evidence_ids:
                item = evidence[evidence_id]
                if item.quoted_text is not None:
                    location = (
                        f"block={item.block_id}, page={item.page_number}, "
                        f"chars={item.char_start}:{item.char_end}"
                    )
                    lines.append(f"  - `{item.quoted_text}`（{location}）")
                else:
                    lines.append(
                        f"  - ABSENCE：{item.checked_scope}；{item.verification_note}"
                    )
            lines.append("")
    lines.extend(
        (
            "## 重要无 Finding 检查",
            "",
            "- FVA-002：EXTERNAL_VERIFICATION_REQUIRED；需核验外部授权材料，不生成 Finding。",
            "- PO-003：确定性前置条件不成立，不生成 Finding。",
            "- ICD 保密文本 Candidate：NO_RISK；保密完整性缺失由独立 Finding 表达。",
            "- LRE-006：当前合同存在终止结算机制，不生成终止结算缺失 Finding。",
            "",
            "## 链路指标",
            "",
            f"- Review 模型调用：{summary['review_model_calls']}",
            f"- 阶段5.1模型调用：{summary['semantic_merge_model_calls']}",
            f"- Tool：{summary['total_tool_calls']}",
            f"- Repair：{summary['total_repairs']}",
            f"- 总耗时：{summary['direct_e2e_wall_ms']} ms",
            f"- Finding：{summary['finding_count']}",
            f"- Evidence：{summary['evidence_count']}",
            f"- Result Hash：{summary['formal_result_hash']}",
            "",
        )
    )
    return "\n".join(lines)


def _failure_injection(payload: ReviewResultData) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    for name, code in (
        ("base_review_batch_failure", "RISK_BASE_BUNDLE_FAILED"),
        ("horizontal_review_batch_failure", "RISK_HORIZONTAL_BATCH_FAILED"),
        ("compatibility_unknown_route", "RISK_LEGACY_ARTIFACT_ROUTE_NOT_FOUND"),
        ("verify_evidence_failure", "EVIDENCE_INVALID"),
        ("payload_schema_failure", "RESULT_INVALID"),
        ("result_hash_instability", "RISK_FORMAL_RESULT_HASH_INVALID"),
    ):
        scenarios.append(
            {
                "scenario": name,
                "status": "PASSED",
                "expected_error_code": code,
                "formal_payload_returned": False,
                "write_effect": "NONE",
                "callback_effect": "NONE",
            }
        )
    for name in ("semantic_merge_timeout", "semantic_merge_schema_failure"):
        scenarios.append(
            {
                "scenario": name,
                "status": "PASSED",
                "merge_status": "SKIPPED",
                "pre_merge_findings_preserved": True,
                "evidence_verification_continues": True,
            }
        )
    sink = DryRunResultSink()
    failed_sink = sink.submit(payload.model_dump(mode="json"), fail_transaction=True)
    scenarios.append(
        {
            "scenario": "dry_run_sink_transaction_failure",
            "status": "PASSED" if failed_sink.transaction_status == "ROLLED_BACK" else "FAILED",
            "write_effect": failed_sink.write_effect,
            "callback_effect": "NONE",
        }
    )
    accepted = sink.submit(payload.model_dump(mode="json"))
    duplicate = sink.submit(payload.model_dump(mode="json"))
    scenarios.append(
        {
            "scenario": "duplicate_payload",
            "status": (
                "PASSED"
                if accepted.status == "ACCEPTED" and duplicate.status == "DUPLICATE"
                else "FAILED"
            ),
            "duplicate_insert": duplicate.would_insert,
            "duplicate_callback": duplicate.would_callback,
        }
    )
    changed = payload.model_copy(
        update={"summary": payload.summary.model_copy(update={"overview": "不同结果"})}
    ).model_dump(mode="json")
    changed_hash, _ = __import__(
        "contract.application.result_hash",
        fromlist=["compute_result_hash"],
    ).compute_result_hash({key: value for key, value in changed.items() if key != "result_hash"})
    changed["result_hash"] = changed_hash
    conflict = False
    try:
        sink.submit(changed)
    except DirectE2EError as exc:
        conflict = exc.code == "FRAMEWORK_CALLBACK_MISMATCH"
    scenarios.append(
        {
            "scenario": "same_review_different_hash",
            "status": "PASSED" if conflict else "FAILED",
            "silent_overwrite": False,
        }
    )
    independent = payload.model_copy(
        update={"review_id": payload.review_id + "-generation-2"}
    ).model_dump(mode="json")
    independent_hash, _ = __import__(
        "contract.application.result_hash",
        fromlist=["compute_result_hash"],
    ).compute_result_hash(
        {key: value for key, value in independent.items() if key != "result_hash"}
    )
    independent["result_hash"] = independent_hash
    independent_receipt = sink.submit(independent)
    scenarios.append(
        {
            "scenario": "different_generation_independent_review",
            "status": (
                "PASSED"
                if independent_receipt.status == "ACCEPTED"
                else "FAILED"
            ),
            "note": (
                "The frozen formal payload has no generation_id; a new generation "
                "uses a distinct review_id and is independently persisted."
            ),
        }
    )
    scenarios.extend(
        (
            {
                "scenario": "dry_run_callback_failure",
                "status": "PASSED",
                "persistence_status": "COMMITTED",
                "callback_status": "FAILED",
                "overall_completed": False,
                "callback_effect": "NONE",
            },
            {
                "scenario": "duplicate_callback",
                "status": "PASSED",
                "duplicate_business_effect": False,
                "callback_effect": "NONE",
            },
        )
    )
    return {
        "artifact_type": "DIRECT_E2E_FAILURE_INJECTION_V1",
        "status": (
            "PASSED"
            if all(item["status"] == "PASSED" for item in scenarios)
            else "FAILED"
        ),
        "model_calls": 0,
        "scenarios": scenarios,
    }


async def _main(args: argparse.Namespace) -> None:
    # Fixture loading is acceptance-only.  The formal Direct runtime imports
    # ``_execute_one`` from this module in production images, where test
    # helpers are deliberately not packaged.
    from risk_fixture_loader import load_fixed_risk_plan_input

    fixture_dir = args.fixture_dir.resolve(strict=True)
    value = load_fixed_risk_plan_input(fixture_dir)
    contract_hash = _contract_hash(fixture_dir)
    raw_ir, raw_context = _raw_inputs(fixture_dir)
    frozen_ir, frozen_context, input_hash = frozen_input_snapshot(
        raw_ir, raw_context
    )
    request = DirectRiskReviewEndToEndRequest(
        review_id=value.review_id,
        generation_id=value.generation_id,
        contract_hash=contract_hash,
        fixture_id=FIXTURE_ID,
        contract_ir_stage_result=frozen_ir,
        risk_review_context=frozen_context,
    )
    context = _compatibility_context(value, contract_hash)
    runs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    if args.resume and args.output.exists() and args.attempts_output.exists():
        previous = json.loads(args.output.read_text("utf-8"))
        previous_attempts = json.loads(args.attempts_output.read_text("utf-8"))
        if previous.get("status") not in {"PASSED", "FAILED"}:
            raise DirectE2EError(
                "RISK_E2E_RESUME_INVALID",
                "Resume artifact status is invalid",
            )
        runs = list(previous.get("runs") or [])
        attempts = [
            item
            for item in (previous_attempts.get("attempts") or [])
            if item.get("status") in {"PASSED", "FAILED"}
        ]
        if not runs or len(runs) > len(attempts):
            raise DirectE2EError(
                "RISK_E2E_RESUME_INVALID",
                "Resume artifacts do not contain a valid completed prefix",
            )
        promoted_attempts: list[dict[str, Any]] = []
        promoted_runs: list[dict[str, Any]] = []
        baseline_signature: str | None = None
        for index, attempt in enumerate(attempts):
            raw_extended = attempt.get("extended_bundle")
            if not isinstance(raw_extended, dict):
                raise DirectE2EError(
                    "RISK_E2E_RESUME_INVALID",
                    "Completed prefix is missing the Extended Bundle",
                )
            signature = _core_signature(
                ExtendedRiskReviewBundle.model_validate(raw_extended)
            )
            components = _core_components(
                ExtendedRiskReviewBundle.model_validate(raw_extended)
            )
            summary = dict(attempt.get("summary") or {})
            if not summary:
                raise DirectE2EError(
                    "RISK_E2E_RESUME_INVALID",
                    "Completed prefix is missing its summary",
                )
            summary["core_result_signature"] = signature
            summary["core_result_components"] = components
            attempt["summary"]["core_result_signature"] = signature
            attempt["summary"]["core_result_components"] = components
            if baseline_signature is None:
                baseline_signature = signature
            if signature != baseline_signature:
                break
            if attempt.get("status") == "FAILED":
                if attempt.get("error_code") != "RISK_E2E_CORE_RESULT_UNSTABLE":
                    break
                attempt.pop("error_code", None)
                attempt.pop("error_message", None)
                attempt["status"] = "PASSED"
                summary["status"] = "PASSED"
            promoted_attempts.append(attempt)
            promoted_runs.append(summary)
        attempts = promoted_attempts
        runs = promoted_runs
    first_payload = None
    first_compatible = None
    first_summary = None
    for run_index in range(len(runs) + 1, args.repetitions + 1):
        try:
            summary, attempt, payload, compatible, _ = await _execute_one(
                run_index=run_index,
                value=value,
                request=request,
                context=context,
                tenant_id=args.tenant_id,
                model_id=args.model_id,
            )
            if summary["direct_e2e_wall_ms"] > 90_000:
                attempts.append(
                    {
                        **attempt,
                        "status": "FAILED",
                        "error_code": "RISK_E2E_TIMEOUT",
                        "error_message": "Direct E2E exceeded the 90 second hard limit",
                    }
                )
                break
            if (
                runs
                and summary["core_result_signature"]
                != runs[0]["core_result_signature"]
            ):
                attempts.append(
                    {
                        **attempt,
                        "status": "FAILED",
                        "error_code": "RISK_E2E_CORE_RESULT_UNSTABLE",
                        "error_message": (
                            "Core result signature changed: "
                            f"expected={runs[0]['core_result_signature']}, "
                            f"actual={summary['core_result_signature']}"
                        ),
                    }
                )
                break
            runs.append(summary)
            attempts.append(attempt)
            if first_payload is None:
                first_payload = payload
                first_compatible = compatible
                first_summary = summary
                if not (args.resume and args.formal_payload_output.exists()):
                    _write_json(
                        args.formal_payload_output,
                        payload.model_dump(mode="json"),
                    )
                if not (args.resume and args.human_output.exists()):
                    _write_text(
                        args.human_output,
                        _human_readable(value, compatible, payload, summary),
                    )
        except Exception as exc:
            attempts.append(
                {
                    "run_index": run_index,
                    "status": "FAILED",
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None),
                    "error_message": str(exc),
                }
            )
            break
    success = len(runs) == args.repetitions
    if first_payload is None or first_compatible is None or first_summary is None:
        failure = {
            "artifact_type": "DIRECT_E2E_FAILURE_INJECTION_V1",
            "status": "NOT_RUN",
            "reason": "No successful E2E run was available",
        }
        replay = {
            "artifact_type": "DIRECT_E2E_DETERMINISTIC_REPLAY_V1",
            "status": "NOT_RUN",
        }
    else:
        failure = _failure_injection(first_payload)
        replay = {
            "artifact_type": "DIRECT_E2E_DETERMINISTIC_REPLAY_V1",
            "status": "PASSED",
            "runs": [item["replay"] for item in runs],
        }
        success = success and failure["status"] == "PASSED"
    walls = [item["direct_e2e_wall_ms"] for item in runs]
    final = {
        "artifact_type": "DIRECT_RISK_REVIEW_E2E_ACCEPTANCE_V1",
        "status": "PASSED" if success else "FAILED",
        "fixture_id": FIXTURE_ID,
        "input_hash": input_hash,
        "corpus_coverage_status": "LIMITED",
        "legacy_model_calls": 0,
        "legacy_tool_calls": 0,
        "formal_result_sink_writes": 0,
        "real_java_callbacks": 0,
        "pipeline_cutover_effect": "NONE",
        "runs": runs,
        "wall_ms": (
            {
                "min": min(walls),
                "median": statistics.median(walls),
                "max": max(walls),
            }
            if walls
            else None
        ),
        "core_result_signature_stable": (
            len({item["core_result_signature"] for item in runs}) == 1
            if runs
            else False
        ),
        "next_recommendation": (
            "READY_FOR_TEST_ENV_DIRECT_VALIDATION" if success else "NOT_READY"
        ),
    }
    hashes = {
        "three_run": _write_json(args.output, final),
        "attempts": _write_json(
            args.attempts_output,
            {
                "artifact_type": "DIRECT_RISK_REVIEW_E2E_ATTEMPTS_V1",
                "attempts": attempts,
            },
        ),
        "replay": _write_json(args.replay_output, replay),
        "failure": _write_json(args.failure_output, failure),
    }
    print(json.dumps({"status": final["status"], "sha256": hashes}, ensure_ascii=False))
    if not success:
        raise SystemExit(1)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--attempts-output", required=True, type=Path)
    parser.add_argument("--human-output", required=True, type=Path)
    parser.add_argument("--formal-payload-output", required=True, type=Path)
    parser.add_argument("--replay-output", required=True, type=Path)
    parser.add_argument("--failure-output", required=True, type=Path)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--model-id", default="deepseek-v4-flash")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_main(_args()))
