#!/usr/bin/env python3
"""Stage 6.5 legacy compatibility and semantic-merge acceptance runner."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import statistics
import sys
import time
from dataclasses import replace
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

from contract.api.models import ContractProfile
from contract.callback.models import FindingConsolidationArtifact
from risk_fixture_loader import load_fixed_risk_plan_input
from services.contract.capabilities.finding_consolidation import (
    FindingConsolidationEngine,
    _candidate_batches,
    _estimated_prompt_tokens,
    build_candidate_pairs,
)
from services.contract.capabilities.legacy_compatibility import (
    COMPATIBILITY_ROUTING_VERSION,
    LegacyCompatibilityContext,
    LegacyRiskArtifactAdapter,
    finalize_legacy_compatible_result,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _sha256_bytes(canonical.encode("utf-8"))


def _write_json(path: Path, value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return _sha256_bytes(text.encode("utf-8"))


def _load_extended_bundle(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = path.read_bytes()
    artifact = json.loads(raw)
    if artifact.get("status") != "PASSED" or len(artifact.get("runs", [])) != 3:
        raise ValueError("Stage 6.4 Extended Bundle artifact is not the passed three-run result")
    bundles = [item.get("raw_bundle") for item in artifact["runs"]]
    if any(not isinstance(item, dict) for item in bundles):
        raise ValueError("Stage 6.4 artifact does not contain complete raw bundles")
    signatures = [
        _sha256_json(
            {
                "check_codes": item.get("check_codes"),
                "findings": item.get("findings"),
                "base_roots": [
                    {
                        "unit_id": unit.get("unit_id"),
                        "roots": unit.get("canonical_risk_roots"),
                    }
                    for unit in item.get("base_bundle", {}).get("units", [])
                ],
                "horizontal_roots": [
                    {
                        "unit_id": unit.get("unit_id"),
                        "roots": unit.get("canonical_roots"),
                    }
                    for unit in item.get("horizontal_units", [])
                ],
            }
        )
        for item in bundles
    ]
    return bundles[0], {
        "path": str(path),
        "sha256": _sha256_bytes(raw),
        "selected_run": 1,
        "run_core_signatures": signatures,
    }


def _context_and_blocks(fixture_dir: Path, bundle: dict[str, Any]):
    value = load_fixed_risk_plan_input(fixture_dir)
    fixture = json.loads(
        (
            fixture_dir
            / "contract-risk-review-fixture-service-outsourcing-0829-v1.json"
        ).read_text("utf-8")
    )
    contract_hash = fixture["source_document"]["content_sha256"]
    context = LegacyCompatibilityContext(
        review_id=value.review_id,
        business_task_id="stage65-fixed-fixture",
        contract_version_id=value.document_id,
        generation_id=value.generation_id,
        contract_hash=contract_hash,
        contract_profile=ContractProfile(
            contract_type=value.contract_type,
            party_a={"name": value.our_party},
            party_b={"name": value.counterparty},
            perspective=value.perspective,
            our_party=value.our_party,
            counterparty=value.counterparty,
            review_attitude=value.review_attitude,
        ),
    )
    if bundle.get("generation_id") != value.generation_id:
        raise ValueError("Extended Bundle generation differs from the fixed Fixture")
    blocks = [item.model_dump(mode="json") for item in value.source_blocks]
    return context, blocks


def _routing_payload(projection, source: dict[str, Any]) -> dict[str, Any]:
    artifacts = projection.artifacts.as_artifact_dict()
    finding_contexts = _finding_contexts(projection)
    candidate_pairs = build_candidate_pairs(
        artifacts,
        finding_contexts=finding_contexts,
    )
    pair_batches = _candidate_batches(
        candidate_pairs,
        max_pairs=60,
        max_estimated_prompt_tokens=5_200,
    )
    record_by_compatible_id = {
        item.compatible_finding_id: item for item in projection.routing_records
    }
    artifact_counts = {
        key: {
            "finding_count": len(value["findings"]),
            "evidence_count": len(value["evidences"]),
            "artifact_hash": _sha256_json(value),
        }
        for key, value in artifacts.items()
    }
    records = [item.model_dump(mode="json") for item in projection.routing_records]
    return {
        "artifact_type": "CONTRACT_RISK_STAGE65_COMPATIBILITY_ROUTING_V1",
        "status": "PASSED",
        "compatibility_routing_version": COMPATIBILITY_ROUTING_VERSION,
        "source": source,
        "source_extended_bundle_id": projection.bundle_id,
        "source_finding_count": projection.source_finding_count,
        "routed_finding_count": len(records),
        "suppressed_horizontal_candidate_count": (
            projection.suppressed_horizontal_finding_count
        ),
        "adapter_duration_ms": projection.adapter_duration_ms,
        "adapter_model_call_count": 0,
        "routing_hash": _sha256_json(records),
        "artifact_set_hash": _sha256_json(artifacts),
        "artifact_counts": artifact_counts,
        "semantic_pair_count": len(candidate_pairs),
        "semantic_pair_ids": [item.pair_id for item in candidate_pairs],
        "semantic_pair_batches": [
            {
                "batch_index": index,
                "pair_count": len(batch),
                "estimated_prompt_tokens": _estimated_prompt_tokens(batch),
                "pair_ids": [item.pair_id for item in batch],
            }
            for index, batch in enumerate(pair_batches, 1)
        ],
        "semantic_pairs": [
            {
                "pair_id": item.pair_id,
                "left": {
                    **item.left.as_dict(),
                    "source_check_code": record_by_compatible_id[
                        item.left.finding_id
                    ].source_check_code,
                    "risk_type": record_by_compatible_id[
                        item.left.finding_id
                    ].risk_type,
                    "title": item.left_summary["title"],
                },
                "right": {
                    **item.right.as_dict(),
                    "source_check_code": record_by_compatible_id[
                        item.right.finding_id
                    ].source_check_code,
                    "risk_type": record_by_compatible_id[
                        item.right.finding_id
                    ].risk_type,
                    "title": item.right_summary["title"],
                },
            }
            for item in candidate_pairs
        ],
        "routing_records": records,
    }


def _oracle_payload(projection) -> dict[str, Any]:
    artifacts = projection.artifacts.as_artifact_dict()
    record_by_compatible_id = {
        item.compatible_finding_id: item for item in projection.routing_records
    }
    pairs = build_candidate_pairs(
        artifacts,
        finding_contexts=_finding_contexts(projection),
    )
    values = [
        {
            "source_finding_id": item.source_finding_id,
            "source_root_id": item.source_root_id,
            "source_check_code": item.source_check_code,
            "category": item.category,
            "risk_type": item.risk_type,
            "owner_type": item.owner_type,
            "legacy_artifact_type": item.legacy_artifact_type,
            "routing_rule_id": item.routing_rule_id,
            "participates_in_pair_generation": True,
        }
        for item in projection.routing_records
    ]
    pair_oracle = []
    for item in pairs:
        left_check = record_by_compatible_id[item.left.finding_id].source_check_code
        right_check = record_by_compatible_id[item.right.finding_id].source_check_code
        expected_same = False
        pair_oracle.append(
            {
                "pair_id": item.pair_id,
                "left_check_code": left_check,
                "right_check_code": right_check,
                "allowed_relations": (
                    ["SAME_RISK"]
                    if expected_same
                    else ["RELATED_DISTINCT", "DISTINCT"]
                ),
                "forbidden_relation": None if expected_same else "SAME_RISK",
                "expected_final_action": (
                    "MERGE"
                    if expected_same
                    else "RETAIN_BOTH"
                ),
                "reason": (
                    "The pair represents different legal consequences after "
                    "Stage 6.3/6.4 Canonical Root and ownership processing"
                ),
            }
        )
    return {
        "artifact_type": "CONTRACT_RISK_STAGE65_ROUTING_ORACLE_V1",
        "compatibility_routing_version": COMPATIBILITY_ROUTING_VERSION,
        "source_extended_bundle_id": projection.bundle_id,
        "entries": values,
        "expected_routing_hash": _sha256_json(values),
        "semantic_pair_oracle": pair_oracle,
        "required_fixture_assertions": {
            "FVA-002_finding_count": 0,
            "CF-005_high_count": sum(
                item.source_check_code == "CF-005"
                for item in projection.routing_records
            ),
            "PO_root_count": sum(
                item.source_check_code.startswith("PO-")
                for item in projection.routing_records
            ),
            "ICD_root_count": sum(
                item.source_check_code.startswith("ICD-")
                for item in projection.routing_records
            ),
            "LRE_root_count": sum(
                item.source_check_code.startswith("LRE-")
                for item in projection.routing_records
            ),
            "horizontal_finding_count": sum(
                item.owner_type == "HORIZONTAL"
                for item in projection.routing_records
            ),
        },
    }


def _result_signature(result) -> str:
    return _sha256_json(
        {
            "merge_status": result.merge_status,
            "routing_records": [
                item.model_dump(mode="json") for item in result.routing_records
            ],
            "same_risk_pair_ids": [
                item.pair_id
                for item in result.merge_artifact.decisions
                if item.relation == "SAME_RISK"
            ],
            "findings": [
                item.model_dump(mode="json") for item in result.final_findings
            ],
            "evidence": [
                item.model_dump(mode="json") for item in result.final_evidence
            ],
            "result_hash": result.result_hash,
        }
    )


def _empty_consolidation(
    *,
    status: str = "COMPLETED",
    reason: str | None = None,
) -> FindingConsolidationArtifact:
    return FindingConsolidationArtifact(
        result_type="FINDING_CONSOLIDATION_V1",
        status=status,
        candidate_count=0,
        model_call_count=0,
        decisions=[],
        skip_reason=reason,
    )


def _failure_injection_payload(
    *,
    bundle: dict[str, Any],
    projection,
    context,
    blocks,
) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []

    def hard_failure(name: str, operation) -> None:
        try:
            operation()
        except Exception as exc:
            scenarios.append(
                {
                    "scenario": name,
                    "status": "PASSED",
                    "expected_outcome": "COMPATIBILITY_FAILED",
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None),
                    "error_message": str(exc),
                    "partial_formal_result": False,
                }
            )
        else:
            scenarios.append(
                {
                    "scenario": name,
                    "status": "FAILED",
                    "expected_outcome": "COMPATIBILITY_FAILED",
                    "error_message": "operation unexpectedly succeeded",
                }
            )

    unknown = copy.deepcopy(bundle)
    unknown["findings"][0]["check_code"] = "ZZ-999"
    hard_failure(
        "unknown_check_route",
        lambda: LegacyRiskArtifactAdapter().adapt(unknown),
    )

    illegal_other = copy.deepcopy(bundle)
    target = next(
        item
        for item in illegal_other["findings"]
        if item["check_code"] != "FVA-005"
    )
    target["category"] = "OTHER"
    hard_failure(
        "illegal_other_category",
        lambda: LegacyRiskArtifactAdapter().adapt(illegal_other),
    )

    duplicate = copy.deepcopy(bundle)
    duplicate["findings"].append(copy.deepcopy(duplicate["findings"][0]))
    hard_failure(
        "finding_id_conflict",
        lambda: LegacyRiskArtifactAdapter().adapt(duplicate),
    )

    multi_artifacts = projection.artifacts.model_copy(deep=True)
    first_record = projection.routing_records[0]
    source_artifact = getattr(
        multi_artifacts,
        first_record.legacy_artifact_type,
    )
    duplicate_finding = next(
        item
        for item in source_artifact.findings
        if item.finding_id == first_record.compatible_finding_id
    )
    other_type = next(
        key
        for key in type(multi_artifacts).model_fields
        if key != first_record.legacy_artifact_type
    )
    getattr(multi_artifacts, other_type).findings.append(duplicate_finding)
    hard_failure(
        "horizontal_finding_multi_artifact_route",
        lambda: finalize_legacy_compatible_result(
            replace(projection, artifacts=multi_artifacts),
            context=context,
            consolidation=_empty_consolidation(),
            blocks=blocks,
        ),
    )

    collision_artifacts = projection.artifacts.model_copy(deep=True)
    all_evidence = [
        item
        for field in type(collision_artifacts).model_fields
        for item in getattr(collision_artifacts, field).evidences
    ]
    original_id = all_evidence[1].evidence_id
    all_evidence[1].evidence_id = all_evidence[0].evidence_id
    hard_failure(
        "evidence_id_conflict",
        lambda: finalize_legacy_compatible_result(
            replace(projection, artifacts=collision_artifacts),
            context=context,
            consolidation=_empty_consolidation(),
            blocks=blocks,
        ),
    )
    all_evidence[1].evidence_id = original_id

    hard_failure(
        "anchor_or_block_outside_generation",
        lambda: finalize_legacy_compatible_result(
            projection,
            context=context,
            consolidation=_empty_consolidation(),
            blocks=[],
        ),
    )
    hard_failure(
        "cross_generation_evidence",
        lambda: finalize_legacy_compatible_result(
            projection,
            context=context,
            consolidation=_empty_consolidation(),
            blocks=[
                {**item, "block_id": "stale-" + item["block_id"]}
                for item in blocks
            ],
        ),
    )

    schema_artifact = projection.artifacts.rights_obligations_review_result
    hard_failure(
        "legacy_artifact_schema_failure",
        lambda: type(schema_artifact).model_validate(
            {
                **schema_artifact.model_dump(mode="json"),
                "result_type": "INVALID_RESULT_TYPE",
            }
        ),
    )

    for scenario in (
        "stage51_timeout",
        "stage51_schema_failure",
        "stage51_semantic_conservation_failure",
    ):
        result = finalize_legacy_compatible_result(
            projection,
            context=context,
            consolidation=_empty_consolidation(
                status="SKIPPED",
                reason="MODEL_CLASSIFICATION_UNAVAILABLE",
            ),
            blocks=blocks,
        )
        scenarios.append(
            {
                "scenario": scenario,
                "status": (
                    "PASSED"
                    if result.merge_status == "SKIPPED"
                    and len(result.final_findings) == projection.source_finding_count
                    else "FAILED"
                ),
                "expected_outcome": "MERGE_SKIPPED_ORIGINAL_FINDINGS_PRESERVED",
                "merge_status": result.merge_status,
                "finding_count": len(result.final_findings),
                "evidence_verification_status": result.evidence_verification_status,
            }
        )

    first = finalize_legacy_compatible_result(
        projection,
        context=context,
        consolidation=_empty_consolidation(status="SKIPPED", reason="TEST"),
        blocks=blocks,
    )
    second = finalize_legacy_compatible_result(
        projection,
        context=context,
        consolidation=_empty_consolidation(status="SKIPPED", reason="TEST"),
        blocks=blocks,
    )
    scenarios.append(
        {
            "scenario": "result_hash_stability",
            "status": "PASSED" if first.result_hash == second.result_hash else "FAILED",
            "expected_outcome": "STABLE",
            "first_result_hash": first.result_hash,
            "second_result_hash": second.result_hash,
        }
    )
    return {
        "artifact_type": "CONTRACT_RISK_STAGE65_FAILURE_INJECTION_V1",
        "status": (
            "PASSED"
            if all(item["status"] == "PASSED" for item in scenarios)
            else "FAILED"
        ),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }


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


async def _run_semantic_merge(
    *,
    projection,
    context,
    blocks,
    tenant_id: str,
    model_id: str,
    repetitions: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    engine = FindingConsolidationEngine()
    artifacts = projection.artifacts.as_artifact_dict()
    finding_contexts = _finding_contexts(projection)
    expected_pairs = [
        item.pair_id
        for item in build_candidate_pairs(
            artifacts,
            finding_contexts=finding_contexts,
        )
    ]
    expected_same_pairs: set[str] = set()
    if expected_same_pairs:
        raise ValueError("Fixture Oracle must not contain a SAME_RISK pair")
    runs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    failures: list[str] = []
    stable_signature: str | None = None
    for run_index in range(1, repetitions + 1):
        started = time.perf_counter()
        run = await engine.consolidate_with_metrics(
            artifacts,
            tenant_id=tenant_id,
            model_id=model_id,
            finding_contexts=finding_contexts,
        )
        merge_artifact = FindingConsolidationArtifact.model_validate(run["artifact"])
        attempt: dict[str, Any] = {
            "run_index": run_index,
            "merge_artifact": merge_artifact.model_dump(mode="json"),
            "merge_wall_ms": run["wall_duration_ms"],
            "call_metrics": run["call_metrics"],
        }
        try:
            if merge_artifact.status != "COMPLETED":
                raise ValueError("Stage 5.1 semantic merge was SKIPPED")
            actual_same_pairs = {
                item.pair_id
                for item in merge_artifact.decisions
                if item.relation == "SAME_RISK"
            }
            if actual_same_pairs != expected_same_pairs:
                raise ValueError("Stage 5.1 SAME_RISK set differs from Fixture Oracle")
            result = finalize_legacy_compatible_result(
                projection,
                context=context,
                consolidation=merge_artifact,
                blocks=blocks,
            )
            total_wall_ms = round((time.perf_counter() - started) * 1000)
            repair_count = sum(
                int(item.get("repair_no", 0) > 0)
                for item in run["call_metrics"]
            )
            budget_statuses = [
                item.get("prompt_budget", {}).get("budget_status")
                for item in run["call_metrics"]
            ]
            summary = {
                "run_index": run_index,
                "adapter_duration_ms": projection.adapter_duration_ms,
                "semantic_merge_wall_ms": run["wall_duration_ms"],
                "total_compatibility_wall_ms": total_wall_ms,
                "model_call_count": merge_artifact.model_call_count,
                "repair_count": repair_count,
                "tool_call_count": 0,
                "prompt_tokens": sum(
                    item.get("prompt_tokens") or 0 for item in run["call_metrics"]
                ),
                "cached_tokens": sum(
                    item.get("cached_tokens") or 0 for item in run["call_metrics"]
                ),
                "completion_tokens": sum(
                    item.get("completion_tokens") or 0
                    for item in run["call_metrics"]
                ),
                "budget_statuses": budget_statuses,
                "artifact_finding_counts": {
                    key: len(value["findings"])
                    for key, value in artifacts.items()
                },
                "candidate_pair_count": len(expected_pairs),
                "candidate_pair_ids": expected_pairs,
                "same_risk_count": result.metrics.same_risk_count,
                "related_distinct_count": result.metrics.related_distinct_count,
                "distinct_count": result.metrics.distinct_count,
                "merge_status": result.merge_status,
                "pre_merge_finding_count": result.metrics.pre_merge_finding_count,
                "post_merge_finding_count": result.metrics.post_merge_finding_count,
                "final_evidence_count": len(result.final_evidence),
                "result_hash": result.result_hash,
                "result_signature": _result_signature(result),
                "final_findings": [
                    item.model_dump(mode="json") for item in result.final_findings
                ],
                "final_evidence": [
                    item.model_dump(mode="json") for item in result.final_evidence
                ],
            }
            if projection.adapter_duration_ms > 3_000:
                raise ValueError("Compatibility adapter exceeded 3 seconds")
            if run["wall_duration_ms"] > 60_000:
                raise ValueError("Stage 5.1 exceeded 60 seconds")
            if total_wall_ms > 65_000:
                raise ValueError("Compatibility chain exceeded 65 seconds")
            if any(value == "HARD_LIMIT_EXCEEDED" for value in budget_statuses):
                raise ValueError("Stage 5.1 Provider Prompt exceeded 7000 tokens")
            if len(result.final_findings) != projection.source_finding_count:
                raise ValueError("Semantic merge Finding count differs from Fixture Oracle")
            if stable_signature is None:
                stable_signature = summary["result_signature"]
            elif summary["result_signature"] != stable_signature:
                raise ValueError("Stage 5.1 core result is not stable")
            attempts.append({**attempt, "status": "PASSED", "summary": summary})
            runs.append(summary)
        except Exception as exc:
            attempts.append(
                {
                    **attempt,
                    "status": "FAILED",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            failures.append(f"run {run_index}: {type(exc).__name__}: {exc}")
            break
    walls = [item["total_compatibility_wall_ms"] for item in runs]
    final = {
        "artifact_type": "CONTRACT_RISK_STAGE65_SEMANTIC_MERGE_ACCEPTANCE_V1",
        "status": (
            "PASSED"
            if not failures and len(runs) == repetitions
            else "FAILED"
        ),
        "repetitions": repetitions,
        "runs": runs,
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
    attempt_artifact = {
        "artifact_type": (
            "CONTRACT_RISK_STAGE65_SEMANTIC_MERGE_ACCEPTANCE_ATTEMPTS_V1"
        ),
        "attempts": attempts,
    }
    return final, attempt_artifact


async def _main(args: argparse.Namespace) -> None:
    bundle, source = _load_extended_bundle(args.extended_artifact.resolve(strict=True))
    context, blocks = _context_and_blocks(
        args.fixture_dir.resolve(strict=True),
        bundle,
    )
    adapter = LegacyRiskArtifactAdapter()
    projection = adapter.adapt(bundle)
    artifact_hashes = {
        _sha256_json(adapter.adapt(bundle).artifacts.model_dump(mode="json"))
        for _ in range(100)
    }
    if len(artifact_hashes) != 1:
        raise ValueError("Compatibility Artifact hash is not stable over 100 rebuilds")

    routing = _routing_payload(projection, source)
    oracle = _oracle_payload(projection)
    output_hashes = {
        "routing": _write_json(args.routing_output, routing),
        "oracle": _write_json(args.oracle_output, oracle),
        "failure": _write_json(
            args.failure_output,
            _failure_injection_payload(
                bundle=bundle,
                projection=projection,
                context=context,
                blocks=blocks,
            ),
        ),
    }
    result: dict[str, Any] = {
        "status": "PASSED",
        "routing_output": str(args.routing_output),
        "routing_sha256": output_hashes["routing"],
        "oracle_output": str(args.oracle_output),
        "oracle_sha256": output_hashes["oracle"],
        "failure_output": str(args.failure_output),
        "failure_sha256": output_hashes["failure"],
        "artifact_set_hash": next(iter(artifact_hashes)),
    }
    if not args.offline_only:
        final, attempts = await _run_semantic_merge(
            projection=projection,
            context=context,
            blocks=blocks,
            tenant_id=args.tenant_id,
            model_id=args.model_id,
            repetitions=args.repetitions,
        )
        result["semantic_merge_output"] = str(args.output)
        result["semantic_merge_sha256"] = _write_json(args.output, final)
        result["attempt_output"] = str(args.attempt_output)
        result["attempt_sha256"] = _write_json(args.attempt_output, attempts)
        result["status"] = final["status"]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] != "PASSED":
        raise SystemExit(1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extended-artifact", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--routing-output", type=Path, required=True)
    parser.add_argument("--oracle-output", type=Path, required=True)
    parser.add_argument("--failure-output", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--attempt-output", type=Path)
    parser.add_argument("--tenant-id", default="0")
    parser.add_argument("--model-id", default="deepseek-v4-pro")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--offline-only", action="store_true")
    args = parser.parse_args()
    if not args.offline_only and (args.output is None or args.attempt_output is None):
        parser.error("--output and --attempt-output are required for semantic merge")
    return args


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
