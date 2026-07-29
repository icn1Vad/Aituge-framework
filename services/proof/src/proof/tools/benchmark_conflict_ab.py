from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time
from typing import Any

import httpx
from pydantic import ValidationError
from aituge_model_config import ModelRuntimeProvider

from capabilities.register import ProofConflictItemOutput
from proof.api.app import _conflict_agent_view
from proof.application.service import ProofService
from proof.config import Settings
from proof.tools.benchmark_conflict_judge import _extract_json
from proof.tools.benchmark_conflict_retrieval import (
    _load_samples,
    _load_units,
    _map_cases,
    _workspace_root,
)


DEFAULT_CASES = {
    "PT-004": "rule_reversal",
    "PT-006": "authority_conflict",
    "PT-009": "process_conflict",
    "PT-012": "rule_reversal",
    "PT-014": "numeric_conflict",
    "PT-015": "numeric_conflict",
    "PT-016": "authority_conflict",
    "PT-035": "process_conflict",
}


def run(output: Path, *, sample_ids: list[str], workers: int) -> dict[str, Any]:
    settings = Settings()
    service = ProofService(settings)
    units = _load_units(service.repository)
    samples = [
        sample
        for sample in _load_samples(
            _workspace_root() / "数据集/work/tmp/sample_registry.json",
            all_conflicts=True,
        )
        if sample.get("sample_id") in sample_ids
    ]
    cases = _map_cases(samples, units)

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_run_case, case, settings): case["sample_id"]
            for case in cases
        }
        for future in as_completed(futures):
            sample_id = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append(
                    {
                        "sample_id": sample_id,
                        "error": type(exc).__name__,
                        "a": _failed_strategy(),
                        "b": {
                            **_failed_strategy(),
                            "verifier_triggered": False,
                            "verifier_succeeded": False,
                        },
                    }
                )
    rows.sort(key=lambda row: sample_ids.index(row["sample_id"]))

    report = {
        "benchmark": {
            "cases": len(rows),
            "sample_ids": sample_ids,
            "model": ModelRuntimeProvider.from_environment().active_pack.llm.id,
            "candidate_limit": 10,
            "a": "single_joint_judge",
            "b": "single_joint_judge_then_conditional_verifier",
        },
        "summary": {
            "a": _summarize(rows, "a"),
            "b": _summarize(rows, "b"),
            "verifier_triggered": sum(row["b"]["verifier_triggered"] for row in rows),
            "verifier_succeeded": sum(row["b"]["verifier_succeeded"] for row in rows),
        },
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _run_case(case: dict[str, Any], settings: Settings) -> dict[str, Any]:
    service = ProofService(settings)
    source_id = str(case["query_units"][0]["id"])
    started = time.perf_counter()
    retrieval = service.retrieve_conflict_candidates(source_id, top_k=10)
    view = _conflict_agent_view(retrieval)
    retrieval_ms = int((time.perf_counter() - started) * 1000)
    skill = _skill_path().read_text(encoding="utf-8")

    primary_messages = [
        {
            "role": "system",
            "content": "\n\n".join(
                [
                    "You are the Proof policy conflict audit judge.",
                    "The evidence is prefetched. Do not call tools.",
                    skill,
                ]
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    "Judge the source against itself and all supplied candidates.",
                    "Return exactly the JSON object required by the skill.",
                    "Prefetched conflict evidence:",
                    json.dumps(view, ensure_ascii=False),
                ]
            ),
        },
    ]
    primary = _call_validated(
        settings,
        service,
        source_id=source_id,
        messages=primary_messages,
        attempts=2,
    )
    primary_output = primary["output"] or ProofConflictItemOutput(findings=[])
    a_metrics = _quality_metrics(primary_output, case, DEFAULT_CASES.get(case["sample_id"]))
    a = {
        **a_metrics,
        "status": primary["status"],
        "attempts": primary["attempts"],
        "tokens": primary["tokens"],
        "elapsed_ms": retrieval_ms + primary["elapsed_ms"],
        "finding_count": len(primary_output.findings),
        "conflict_types": [finding.conflict_type for finding in primary_output.findings],
        "findings": _finding_summaries(primary_output),
    }

    triggered = primary["status"] == "succeeded" and _needs_verification(primary_output)
    verifier = {
        "status": "not_triggered",
        "attempts": 0,
        "tokens": 0,
        "elapsed_ms": 0,
        "output": None,
    }
    if triggered:
        evidence_view = _implicated_evidence(view, primary_output)
        verifier_messages = [
            {
                "role": "system",
                "content": "\n\n".join(
                    [
                        "You are the second-pass verifier for policy conflict findings.",
                        "Confirm, correct, merge, or remove the proposed findings using only the supplied original evidence.",
                        "Correct the primary conflict_type when another of the four types is the direct cause.",
                        "Return the same strict JSON object required by this skill.",
                        skill,
                    ]
                ),
            },
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "Verify the proposed findings. Do not preserve an unsupported finding.",
                        "Every candidate_ids entry must have an evidence item with the identical unit_id.",
                        "Proposed findings:",
                        json.dumps(primary_output.model_dump(), ensure_ascii=False),
                        "Original evidence:",
                        json.dumps(evidence_view, ensure_ascii=False),
                    ]
                ),
            },
        ]
        verifier = _call_validated(
            settings,
            service,
            source_id=source_id,
            messages=verifier_messages,
            attempts=2,
        )

    final_output = verifier["output"] or primary_output
    b_metrics = _quality_metrics(final_output, case, DEFAULT_CASES.get(case["sample_id"]))
    b = {
        **b_metrics,
        "status": "succeeded" if primary["status"] == "succeeded" else primary["status"],
        "attempts": primary["attempts"] + verifier["attempts"],
        "tokens": primary["tokens"] + verifier["tokens"],
        "elapsed_ms": retrieval_ms + primary["elapsed_ms"] + verifier["elapsed_ms"],
        "finding_count": len(final_output.findings),
        "conflict_types": [finding.conflict_type for finding in final_output.findings],
        "findings": _finding_summaries(final_output),
        "verifier_triggered": triggered,
        "verifier_succeeded": verifier["status"] == "succeeded",
        "verifier_status": verifier["status"],
    }
    return {
        "sample_id": case["sample_id"],
        "error_type": case["error_type"],
        "expected_conflict_type": DEFAULT_CASES.get(case["sample_id"]),
        "retrieval_ms": retrieval_ms,
        "candidate_count": len(view.get("results") or []),
        "a": a,
        "b": b,
    }


def _call_validated(
    settings: Settings,
    service: ProofService,
    *,
    source_id: str,
    messages: list[dict[str, str]],
    attempts: int,
) -> dict[str, Any]:
    total_tokens = 0
    elapsed_ms = 0
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            response = httpx.post(
                settings.embedding_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={
                    "model": (
                        ModelRuntimeProvider.from_environment()
                        .active_pack.llm.id
                    ),
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 8000,
                    "stream": False,
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=180,
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage") or {}
            total_tokens += int(usage.get("total_tokens") or 0)
            content = str(body["choices"][0]["message"].get("content") or "")
            output = ProofConflictItemOutput.model_validate(_extract_json(content))
            for finding in output.findings:
                service._validate_conflict_finding(
                    finding.model_dump(),
                    target_ids={source_id},
                )
            elapsed_ms += int((time.perf_counter() - started) * 1000)
            return {
                "status": "succeeded",
                "attempts": attempt,
                "tokens": total_tokens,
                "elapsed_ms": elapsed_ms,
                "errors": errors,
                "output": output,
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
            elapsed_ms += int((time.perf_counter() - started) * 1000)
            errors.append(type(exc).__name__)
    return {
        "status": "failed",
        "attempts": attempts,
        "tokens": total_tokens,
        "elapsed_ms": elapsed_ms,
        "errors": errors,
        "output": None,
    }


def _needs_verification(output: ProofConflictItemOutput) -> bool:
    if not output.findings:
        return False
    types = {finding.conflict_type for finding in output.findings}
    return (
        len(types) > 1
        or any(finding.confidence < 0.85 for finding in output.findings)
        or any(finding.severity == "high" for finding in output.findings)
        or any(finding.mechanism != "direct" for finding in output.findings)
    )


def _implicated_evidence(view: dict[str, Any], output: ProofConflictItemOutput) -> dict[str, Any]:
    implicated_ids = {
        unit_id
        for finding in output.findings
        for unit_id in [*finding.candidate_ids, *(item.unit_id for item in finding.evidence)]
    }
    source = view.get("source") or {}
    results = [
        item
        for item in view.get("results") or []
        if str(item.get("id")) in implicated_ids
    ][:5]
    return {"source": source, "results": results}


def _quality_metrics(
    output: ProofConflictItemOutput,
    case: dict[str, Any],
    expected_type: str | None,
) -> dict[str, Any]:
    evidence_ids = {
        evidence.unit_id
        for finding in output.findings
        for evidence in finding.evidence
    }
    target_groups = [
        {str(item["id"]) for item in group}
        for group in case["target_groups"]
    ]
    group_hits = [bool(group & evidence_ids) for group in target_groups]
    types = {finding.conflict_type for finding in output.findings}
    return {
        "detected": bool(output.findings),
        "expected_type_hit": expected_type in types if expected_type else None,
        "any_gold_group_cited": any(group_hits),
        "all_gold_groups_cited": all(group_hits),
    }


def _finding_summaries(output: ProofConflictItemOutput) -> list[dict[str, Any]]:
    return [
        {
            "conflict_type": finding.conflict_type,
            "mechanism": finding.mechanism,
            "candidate_ids": finding.candidate_ids,
            "confidence": finding.confidence,
            "matter": finding.matter,
        }
        for finding in output.findings
    ]


def _summarize(rows: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    values = [row[strategy] for row in rows]
    return {
        "schema_success": sum(item["status"] == "succeeded" for item in values) / len(values),
        "case_detection": sum(item["detected"] for item in values) / len(values),
        "expected_type_accuracy": sum(item["expected_type_hit"] for item in values) / len(values),
        "any_gold_group_citation": sum(item["any_gold_group_cited"] for item in values) / len(values),
        "all_gold_groups_citation": sum(item["all_gold_groups_cited"] for item in values) / len(values),
        "total_tokens": sum(item["tokens"] for item in values),
        "mean_tokens": sum(item["tokens"] for item in values) / len(values),
        "mean_elapsed_ms": sum(item["elapsed_ms"] for item in values) / len(values),
    }


def _failed_strategy() -> dict[str, Any]:
    return {
        "status": "failed",
        "detected": False,
        "expected_type_hit": False,
        "any_gold_group_cited": False,
        "all_gold_groups_cited": False,
        "tokens": 0,
        "elapsed_ms": 0,
    }


def _skill_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "capabilities"
        / "skills"
        / "proof-policy-conflict-audit"
        / "SKILL.md"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare one-pass and conditional-verifier conflict audits.")
    parser.add_argument(
        "--sample-ids",
        default=",".join(DEFAULT_CASES),
        help="Comma-separated PT sample IDs.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/conflict-method-eval/conflict-judge-ab-benchmark.json"),
    )
    args = parser.parse_args()
    sample_ids = [value.strip() for value in args.sample_ids.split(",") if value.strip()]
    report = run(args.output, sample_ids=sample_ids, workers=args.workers)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
