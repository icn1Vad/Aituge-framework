#!/usr/bin/env python3
"""Stage 6.4 fixed-fixture horizontal and extended Bundle acceptance runner."""

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

from contract.risk.plan_builder import RiskReviewPlanBuilder
from risk_fixture_loader import load_fixed_risk_plan_input
from services.contract.capabilities.horizontal_review import (
    HorizontalReviewError,
    build_extended_bundle,
    build_horizontal_plan,
    execute_horizontal_phase,
    execute_horizontal_unit,
)
from services.contract.capabilities.risk_review_bundle import (
    BaseBundleExecutionError,
    BaseRiskReviewBundle,
    GenericAttemptArtifact,
    GenericBaseDirectReviewer,
    execute_base_risk_review_bundle,
    generic_request_from_context,
)


FIXTURE_ID = "service-outsourcing-0829-v1"
EXPECTED_HORIZONTAL_TYPES = {
    "cross_clause_consistency": {
        "DATE_CHRONOLOGY_CONFLICT",
        "PARTY_NAME_CONFLICT",
    },
    "missing_ambiguity_completeness": {
        "MISSING_REFERENCED_ATTACHMENT",
    },
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base_bundle(path: Path) -> BaseRiskReviewBundle:
    payload = json.loads(path.read_text("utf-8"))
    return BaseRiskReviewBundle.model_validate(payload["raw_bundles"][0])


def _contract_hash(fixture_dir: Path) -> str:
    payload = json.loads(
        (
            fixture_dir
            / "contract-risk-review-fixture-service-outsourcing-0829-v1.json"
        ).read_text("utf-8")
    )
    return payload["source_document"]["content_sha256"]


def _unit_summary(unit) -> dict[str, Any]:
    return {
        "unit_id": unit.unit_id,
        "wall_duration_ms": unit.wall_duration_ms,
        "model_call_count": unit.model_call_count,
        "repair_count": unit.repair_count,
        "tool_call_count": unit.tool_call_count,
        "batch_metrics": [
            item.model_dump(mode="json") for item in unit.batch_metrics
        ],
        "decisions": [item.model_dump(mode="json") for item in unit.decisions],
        "canonical_roots": [
            item.model_dump(mode="json") for item in unit.canonical_roots
        ],
        "findings": [item.model_dump(mode="json") for item in unit.findings],
        "check_results": [
            item.model_dump(mode="json") for item in unit.check_results
        ],
    }


class _AttemptCollector:
    def __init__(self) -> None:
        self.values: list[dict[str, Any]] = []

    def __call__(self, value: GenericAttemptArtifact) -> None:
        self.values.append(value.model_dump(mode="json"))


def _fva_context(risk_plan):
    matches = [
        context
        for context in risk_plan.contexts
        if str(getattr(context.unit_id, "value", context.unit_id))
        == "formation_validity_authority"
    ]
    if len(matches) != 1:
        raise RuntimeError("Risk Plan must contain exactly one FVA Batch")
    return matches[0]


def _validate_fva_result(result) -> list[str]:
    failures: list[str] = []
    assessments = [
        item for item in result.fva_assessments if item.check_code == "FVA-002"
    ]
    if len(assessments) != 1:
        failures.append("FVA-002 assessment is missing or duplicated")
    elif (
        assessments[0].assessment_type != "EXTERNAL_VERIFICATION_REQUIRED"
        or not assessments[0].external_verification_required
    ):
        failures.append("FVA-002 did not remain external verification")
    if any(item.check_code == "FVA-002" for item in result.findings):
        failures.append("FVA-002 produced a Finding")
    if result.model_call_count != 1:
        failures.append("FVA model-call count is not 1")
    if result.repair_count != 0:
        failures.append("FVA Repair count is non-zero")
    if result.tool_call_count != 0:
        failures.append("FVA Tool count is non-zero")
    return failures


def _validate_base_bundle(base: BaseRiskReviewBundle) -> list[str]:
    failures: list[str] = []
    units = {item.unit_id: item for item in base.units}
    if len(units) != 5:
        failures.append("Base Bundle does not contain exactly five Units")
        return failures
    checks = [
        item.check_code for unit in base.units for item in unit.check_results
    ]
    if len(checks) != 34 or len(set(checks)) != 34:
        failures.append("Base Bundle does not contain 34 unique Checks")

    fva = units["formation_validity_authority"]
    fva_assessments = [
        item for item in fva.fva_assessments if item.check_code == "FVA-002"
    ]
    if (
        len(fva_assessments) != 1
        or fva_assessments[0].assessment_type
        != "EXTERNAL_VERIFICATION_REQUIRED"
        or not fva_assessments[0].external_verification_required
    ):
        failures.append("Base FVA-002 Oracle changed")
    if any(item.check_code == "FVA-002" for item in fva.findings):
        failures.append("Base FVA-002 produced a Finding")

    commercial = units["commercial_financial"]
    if not any(
        item.check_code == "CF-005" and item.risk_level == "HIGH"
        for item in commercial.findings
    ):
        failures.append("Base CF-005 HIGH Oracle changed")

    po = units["performance_obligations"]
    if len(po.canonical_risk_roots) != 6:
        failures.append("Base PO Canonical Root count changed")
    icd = units["ip_confidentiality_data"]
    if len(icd.canonical_risk_roots) != 1:
        failures.append("Base ICD Canonical Root count changed")
    lre = units["liability_remedies_exit"]
    if len(lre.canonical_risk_roots) != 4:
        failures.append("Base LRE Canonical Root count changed")
    if base.metrics.peak_concurrency != 7:
        failures.append("Base Bundle peak concurrency is not 7")
    if base.metrics.model_call_count != 7:
        failures.append("Base Bundle model-call count is not 7")
    if base.metrics.repair_count != 0:
        failures.append("Base Bundle Repair count is non-zero")
    return failures


def _extended_signature(summary: dict[str, Any]) -> tuple[Any, ...]:
    raw = summary["raw_bundle"]
    base = raw["base_bundle"]
    return (
        tuple(
            sorted(
                (
                    unit["unit_id"],
                    tuple(
                        sorted(
                            (
                                root["root_type"],
                                root["risk_level"],
                                tuple(root["primary_evidence_source_ids"]),
                            )
                            for root in unit["canonical_risk_roots"]
                        )
                    ),
                )
                for unit in base["units"]
            )
        ),
        tuple(
            sorted(
                (
                    unit["unit_id"],
                    tuple(
                        sorted(
                            (
                                root["root_type"],
                                root["risk_level"],
                                tuple(root["primary_evidence_source_ids"]),
                            )
                            for root in unit["canonical_roots"]
                        )
                    ),
                )
                for unit in raw["horizontal_units"]
            )
        ),
    )


async def _run_fva(args, value) -> dict[str, Any]:
    risk_plan = RiskReviewPlanBuilder().build(value)
    context = _fva_context(risk_plan)
    request = generic_request_from_context(context)
    runs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    failures: list[str] = []
    for run_index in range(1, args.repetitions + 1):
        collector = _AttemptCollector()
        try:
            result = await GenericBaseDirectReviewer().review(
                request,
                tenant_id=args.tenant_id,
                model_id=args.model_id,
                framework_run_id=f"stage64-fva-regression-{run_index}",
                attempt_artifact_sink=collector,
            )
            round_failures = _validate_fva_result(result)
            summary = {
                "run_index": run_index,
                "duration_ms": result.duration_ms,
                "model_call_count": result.model_call_count,
                "repair_count": result.repair_count,
                "tool_call_count": result.tool_call_count,
                "prompt_tokens": result.prompt_tokens,
                "cached_tokens": result.cached_tokens,
                "completion_tokens": result.completion_tokens,
                "fva_assessments": [
                    item.model_dump(mode="json")
                    for item in result.fva_assessments
                ],
                "fva002_finding_count": len(
                    [
                        item
                        for item in result.findings
                        if item.check_code == "FVA-002"
                    ]
                ),
                "input_diagnostics": (
                    collector.values[0].get("input_diagnostics")
                    if collector.values
                    else None
                ),
            }
            attempts.append(
                {
                    **summary,
                    "attempt_artifacts": collector.values,
                    "failures": round_failures,
                }
            )
            if round_failures:
                failures.extend(round_failures)
                break
            runs.append(summary)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            attempts.append(
                {
                    "run_index": run_index,
                    "status": "FAILED",
                    "error_code": code,
                    "error_message": str(exc),
                    "attempt_artifacts": collector.values,
                }
            )
            failures.append(f"run {run_index}: {code}: {exc}")
            break
    prompt_hashes = {
        item["input_diagnostics"]["serialized_prompt_hash"]
        for item in runs
        if item["input_diagnostics"] is not None
    }
    if len(prompt_hashes) > 1:
        failures.append("FVA serialized Prompt Hash changed across runs")
    return {
        "artifact_type": "CONTRACT_RISK_STAGE64_FVA_REGRESSION_V1",
        "phase": "fva",
        "status": (
            "PASSED"
            if not failures and len(runs) == args.repetitions
            else "FAILED"
        ),
        "plan_id": risk_plan.plan_id,
        "plan_hash": risk_plan.plan_hash,
        "batch_id": context.batch_id,
        "runs": runs,
        "attempts": attempts,
        "failures": failures,
    }


def _validate_unit(unit, plan) -> list[str]:
    failures: list[str] = []
    expected_checks = {
        item for item in plan.check_codes if item.startswith(
            "CCC-" if unit.unit_id == "cross_clause_consistency" else "MAC-"
        )
    }
    if {item.check_code for item in unit.check_results} != expected_checks:
        failures.append(f"{unit.unit_id}: check coverage differs from Registry")
    expected_candidates = {
        item.candidate_id
        for item in plan.candidates
        if item.unit_id == unit.unit_id and item.requires_model_decision
    }
    model_decisions = {
        item.candidate_id
        for item in unit.decisions
        if item.owner_type == "HORIZONTAL"
    }
    if model_decisions != expected_candidates:
        failures.append(f"{unit.unit_id}: Candidate decisions are incomplete")
    candidate_by_id = {item.candidate_id: item for item in plan.candidates}
    risk_types = {
        candidate_by_id[item.candidate_id].candidate_type
        for item in unit.decisions
        if item.owner_type == "HORIZONTAL" and item.verdict == "RISK"
    }
    if risk_types != EXPECTED_HORIZONTAL_TYPES[unit.unit_id]:
        failures.append(
            f"{unit.unit_id}: fixture risk types {sorted(risk_types)} "
            f"!= {sorted(EXPECTED_HORIZONTAL_TYPES[unit.unit_id])}"
        )
    if unit.repair_count or unit.tool_call_count:
        failures.append(f"{unit.unit_id}: Repair or Tool count is non-zero")
    if unit.wall_duration_ms > 60_000:
        failures.append(f"{unit.unit_id}: wall time exceeded 60 seconds")
    if any(
        metric.prompt_budget
        and metric.prompt_budget.budget_status == "HARD_LIMIT_EXCEEDED"
        for metric in unit.batch_metrics
    ):
        failures.append(f"{unit.unit_id}: Provider Prompt exceeded 7,000")
    return failures


def _stability_failures(runs: list[dict[str, Any]]) -> list[str]:
    if len(runs) < 2:
        return []
    keys = []
    for run in runs:
        unit = run["unit"]
        keys.append(
            (
                tuple(
                    sorted(
                        (
                            item["candidate_id"],
                            item["verdict"],
                            tuple(item["accepted_severity_factors"]),
                            item["owner_type"],
                        )
                        for item in unit["decisions"]
                    )
                ),
                tuple(
                    sorted(
                        (
                            item["root_type"],
                            item["risk_level"],
                            tuple(item["primary_evidence_source_ids"]),
                        )
                        for item in unit["canonical_roots"]
                    )
                ),
                tuple(
                    sorted(
                        (
                            item["check_code"],
                            item["risk_type"],
                            item["risk_level"],
                        )
                        for item in unit["findings"]
                    )
                ),
            )
        )
    return [] if all(item == keys[0] for item in keys[1:]) else [
        "Horizontal results are not stable across successful runs"
    ]


async def _run_unit(args, value, base, plan) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    failures: list[str] = []
    for run_index in range(1, args.repetitions + 1):
        try:
            unit = await execute_horizontal_unit(
                value,
                plan,
                args.unit_id,
                tenant_id=args.tenant_id,
                model_id=args.model_id,
                framework_run_id=f"stage64-{args.unit_id}-{run_index}",
            )
            round_failures = _validate_unit(unit, plan)
            summary = {"run_index": run_index, "unit": _unit_summary(unit)}
            attempts.append({**summary, "failures": round_failures})
            if round_failures:
                failures.extend(round_failures)
                break
            runs.append(summary)
            stability = _stability_failures(runs)
            if stability:
                failures.extend(stability)
                break
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            attempts.append(
                {
                    "run_index": run_index,
                    "status": "FAILED",
                    "error_code": code,
                    "error_message": str(exc),
                }
            )
            failures.append(f"run {run_index}: {code}: {exc}")
            break
    return {
        "artifact_type": "CONTRACT_RISK_STAGE64_HORIZONTAL_ACCEPTANCE_V1",
        "phase": args.unit_id,
        "status": "PASSED" if not failures and len(runs) == args.repetitions else "FAILED",
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "relationship_index": plan.relationship_index.model_dump(mode="json"),
        "candidate_count": len(
            [item for item in plan.candidates if item.unit_id == args.unit_id]
        ),
        "batch_count": len(
            [item for item in plan.batches if item.unit_id == args.unit_id]
        ),
        "runs": runs,
        "attempts": attempts,
        "failures": failures,
    }


async def _run_extended(args, value, plan, contract_hash: str) -> dict[str, Any]:
    risk_plan = RiskReviewPlanBuilder().build(value)
    runs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    failures: list[str] = []
    for run_index in range(1, args.repetitions + 1):
        collector = _AttemptCollector()
        try:
            base_started = time.perf_counter()
            base = await execute_base_risk_review_bundle(
                risk_plan,
                tenant_id=args.tenant_id,
                model_id=args.model_id,
                contract_hash=contract_hash,
                fixture_id=FIXTURE_ID,
                framework_run_id=f"stage64-extended-base-{run_index}",
                attempt_artifact_sink=collector,
            )
            base_wall = round((time.perf_counter() - base_started) * 1000)
            candidate_started = time.perf_counter()
            horizontal_plan = build_horizontal_plan(value, base)
            candidate_wall = round(
                (time.perf_counter() - candidate_started) * 1000
            )
            horizontal_started = time.perf_counter()
            units, peak = await execute_horizontal_phase(
                value,
                horizontal_plan,
                tenant_id=args.tenant_id,
                model_id=args.model_id,
                framework_run_id=f"stage64-extended-horizontal-{run_index}",
            )
            horizontal_wall = round(
                (time.perf_counter() - horizontal_started) * 1000
            )
            extended = build_extended_bundle(
                value=value,
                base_bundle=base,
                horizontal_plan=horizontal_plan,
                horizontal_units=units,
                base_phase_wall_ms=base_wall,
                horizontal_candidate_build_ms=candidate_wall,
                horizontal_phase_wall_ms=horizontal_wall,
                horizontal_peak_concurrency=peak,
            )
            round_failures = _validate_base_bundle(base) + [
                failure
                for unit in units
                for failure in _validate_unit(unit, horizontal_plan)
            ]
            if extended.metrics.extended_bundle_wall_ms > 90_000:
                round_failures.append("Extended Bundle exceeded 90 seconds")
            if extended.metrics.model_call_count != len(base.batch_results) + len(
                horizontal_plan.batches
            ):
                round_failures.append("Extended Bundle model-call budget mismatch")
            summary = {
                "run_index": run_index,
                "metrics": extended.metrics.model_dump(mode="json"),
                "base_units": [
                    {
                        "unit_id": unit.unit_id,
                        "root_count": len(unit.canonical_risk_roots),
                        "finding_count": len(unit.findings),
                    }
                    for unit in base.units
                ],
                "horizontal_units": [_unit_summary(unit) for unit in units],
                "finding_count": len(extended.findings),
                "raw_bundle": extended.model_dump(mode="json"),
            }
            attempts.append(
                {
                    **summary,
                    "base_attempt_artifacts": collector.values,
                    "failures": round_failures,
                }
            )
            if round_failures:
                failures.extend(round_failures)
                break
            runs.append(summary)
            if len(runs) > 1 and _extended_signature(runs[-1]) != _extended_signature(
                runs[0]
            ):
                failures.append("Extended Bundle core results are not stable")
                break
        except (BaseBundleExecutionError, HorizontalReviewError, Exception) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            attempts.append(
                {
                    "run_index": run_index,
                    "status": "FAILED",
                    "error_code": code,
                    "error_message": str(exc),
                    "base_attempt_artifacts": collector.values,
                }
            )
            failures.append(f"run {run_index}: {code}: {exc}")
            break
    walls = [item["metrics"]["extended_bundle_wall_ms"] for item in runs]
    return {
        "artifact_type": "CONTRACT_RISK_STAGE64_EXTENDED_ACCEPTANCE_V1",
        "phase": "extended",
        "status": "PASSED" if not failures and len(runs) == args.repetitions else "FAILED",
        "runs": runs,
        "attempts": attempts,
        "duration_ms": (
            {
                "min": min(walls),
                "median": statistics.median(walls),
                "max": max(walls),
            }
            if walls
            else None
        ),
        "failures": failures,
    }


async def _main(args) -> None:
    value = load_fixed_risk_plan_input(args.fixture_dir.resolve(strict=True))
    base = _base_bundle(args.base_artifact.resolve(strict=True))
    plan = build_horizontal_plan(value, base)
    if args.phase == "fva":
        artifact = await _run_fva(args, value)
    elif args.phase == "extended":
        artifact = await _run_extended(
            args,
            value,
            plan,
            _contract_hash(args.fixture_dir),
        )
    else:
        artifact = await _run_unit(args, value, base, plan)
    text = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, "utf-8")
    attempts = {
        "artifact_type": artifact["artifact_type"] + "_ATTEMPTS",
        "phase": artifact["phase"],
        "attempts": artifact["attempts"],
    }
    attempt_text = (
        json.dumps(attempts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    args.attempt_output.parent.mkdir(parents=True, exist_ok=True)
    args.attempt_output.write_text(attempt_text, "utf-8")
    print(
        json.dumps(
            {
                "status": artifact["status"],
                "output": str(args.output),
                "output_sha256": _sha256(text),
                "attempt_output": str(args.attempt_output),
                "attempt_sha256": _sha256(attempt_text),
                "failures": artifact["failures"],
            },
            ensure_ascii=False,
        )
    )
    if artifact["status"] != "PASSED":
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=(
            "cross_clause_consistency",
            "missing_ambiguity_completeness",
            "fva",
            "extended",
        ),
        required=True,
    )
    parser.add_argument("--unit-id")
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--base-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempt-output", type=Path, required=True)
    parser.add_argument("--tenant-id", default="0")
    parser.add_argument("--model-id", default="deepseek-v4-pro")
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.phase not in {"fva", "extended"}:
        args.unit_id = args.phase
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
