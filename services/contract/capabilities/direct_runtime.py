"""Formal Direct structured contract-risk runtime."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from contract.application.idempotency import canonical_json
from contract.risk.models import RiskReviewPlanInput
from contract.risk.plan_builder import RiskReviewPlanBuilder

try:
    from services.contract.capabilities.horizontal_review import (
        ExtendedRiskReviewBundle,
        build_extended_bundle,
        build_horizontal_plan,
        execute_horizontal_phase,
    )
    from services.contract.capabilities.risk_review_bundle import (
        execute_base_risk_review_bundle,
    )
except ModuleNotFoundError as exc:
    if exc.name != "services":
        raise
    from horizontal_review import (
        ExtendedRiskReviewBundle,
        build_extended_bundle,
        build_horizontal_plan,
        execute_horizontal_phase,
    )
    from risk_review_bundle import execute_base_risk_review_bundle


class DirectRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _budget_failures(metrics: list[Any]) -> list[str]:
    failures: list[str] = []
    for item in metrics:
        raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        budget = raw.get("prompt_budget") or {}
        if budget.get("budget_status") == "HARD_LIMIT_EXCEEDED":
            failures.append(str(raw.get("batch_id") or "unknown"))
    return failures


def _core_signature(bundle: ExtendedRiskReviewBundle) -> str:
    components = [
        {
            "finding_id": finding.finding_local_id,
            "check_code": finding.check_code,
            "risk_type": finding.risk_type,
            "risk_level": finding.risk_level,
            "evidence_ids": sorted(
                item.evidence_local_id for item in finding.evidence_candidates
            ),
        }
        for finding in bundle.findings
    ]
    return "sha256:" + hashlib.sha256(
        canonical_json(
            {
                "check_codes": sorted(bundle.check_codes),
                "findings": sorted(components, key=lambda item: item["finding_id"]),
            }
        ).encode("utf-8")
    ).hexdigest()


async def execute_direct_bundle(
    *,
    value: RiskReviewPlanInput,
    tenant_id: str,
    model_id: str,
    contract_hash: str,
    fixture_id: str,
    framework_run_id: str,
    allow_dynamic_base_batch_count: bool = True,
) -> tuple[ExtendedRiskReviewBundle, dict[str, Any]]:
    started = time.perf_counter()
    plan = RiskReviewPlanBuilder().build(value)

    base_started = time.perf_counter()
    base = await execute_base_risk_review_bundle(
        plan,
        tenant_id=tenant_id,
        model_id=model_id,
        contract_hash=contract_hash,
        fixture_id=fixture_id,
        framework_run_id=f"{framework_run_id}-base",
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
        framework_run_id=f"{framework_run_id}-horizontal",
    )
    horizontal_wall = round((time.perf_counter() - horizontal_started) * 1000)
    bundle = build_extended_bundle(
        value=value,
        base_bundle=base,
        horizontal_plan=horizontal_plan,
        horizontal_units=horizontal_units,
        base_phase_wall_ms=base_wall,
        horizontal_candidate_build_ms=horizontal_build_wall,
        horizontal_phase_wall_ms=horizontal_wall,
        horizontal_peak_concurrency=horizontal_peak,
    )
    if bundle.status != "COMPLETED":
        raise DirectRuntimeError(
            "FRAMEWORK_RUN_FAILED",
            "Direct risk review returned an incomplete Extended Bundle",
        )

    review_metrics = [
        metric for unit in base.units for metric in unit.call_metrics
    ] + [
        metric
        for unit in horizontal_units
        for metric in unit.batch_metrics
    ]
    budget_failures = _budget_failures(review_metrics)
    if budget_failures:
        raise DirectRuntimeError(
            "FRAMEWORK_RUN_FAILED",
            "Prompt hard limit exceeded: " + ",".join(budget_failures),
        )

    summary = {
        "direct_bundle_wall_ms": round((time.perf_counter() - started) * 1000),
        "extended_bundle_wall_ms": bundle.metrics.extended_bundle_wall_ms,
        "base_phase_wall_ms": base_wall,
        "horizontal_candidate_build_ms": horizontal_build_wall,
        "horizontal_phase_wall_ms": horizontal_wall,
        "total_model_calls": bundle.metrics.model_call_count,
        "total_repairs": bundle.metrics.repair_count,
        "total_tool_calls": bundle.metrics.tool_call_count,
        "core_result_signature": _core_signature(bundle),
    }
    return bundle, summary
