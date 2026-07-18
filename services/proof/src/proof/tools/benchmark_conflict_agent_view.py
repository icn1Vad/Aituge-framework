from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from proof.api.app import _conflict_agent_view
from proof.application.service import ProofService
from proof.config import Settings
from proof.tools.benchmark_conflict_pipeline import _summarize
from proof.tools.benchmark_conflict_retrieval import (
    _load_samples,
    _load_units,
    _map_cases,
    _rank_for_group,
    _workspace_root,
)


def run(output: Path, *, judge_limit: int) -> dict[str, Any]:
    settings = Settings()
    service = ProofService(settings)
    units = _load_units(service.repository)
    samples = [
        sample
        for sample in _load_samples(
            _workspace_root() / "数据集/work/tmp/sample_registry.json",
            all_conflicts=True,
        )
        if str(sample.get("sample_id") or "").startswith("PT-")
    ]
    cases = _map_cases(samples, units)
    rows: list[dict[str, Any]] = []
    for case in cases:
        query_unit = case["query_units"][0]
        full = service.retrieve_conflict_candidates(str(query_unit["id"]), top_k=judge_limit)
        results = _conflict_agent_view(full)["results"][:judge_limit]
        ranks = [_rank_for_group(results, group) for group in case["target_groups"]]
        finite = [rank for rank in ranks if rank is not None]
        rows.append(
            {
                "sample_id": case["sample_id"],
                "error_type": case["error_type"],
                "target_group_ranks": ranks,
                "best_rank": min(finite) if finite else None,
            }
        )
    report = {
        "benchmark": {
            "cases": len(rows),
            "retrieval_top_k": judge_limit,
            "judge_limit": judge_limit,
            "reranker_run": settings.reranker_configured,
            "judge_order": "service_evidence_order",
        },
        "summary": _summarize(rows),
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the compact conflict Agent evidence view.")
    parser.add_argument("--judge-limit", type=int, default=10)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/conflict-method-eval/conflict-agent-view-benchmark.json"),
    )
    args = parser.parse_args()
    print(json.dumps(run(args.output, judge_limit=args.judge_limit)["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
