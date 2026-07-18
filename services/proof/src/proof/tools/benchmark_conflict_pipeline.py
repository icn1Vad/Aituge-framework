from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from proof.application.conflict_retrieval import ConflictRetrievalService
from proof.config import Settings
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.postgres.repository import ProofRepository
from proof.tools.benchmark_conflict_retrieval import (
    KS,
    _load_samples,
    _load_units,
    _map_cases,
    _rank_for_group,
    _workspace_root,
)


def run(output: Path) -> dict[str, Any]:
    settings = Settings()
    repository = ProofRepository(settings)
    units = _load_units(repository)
    samples = _load_samples(
        _workspace_root() / "数据集/work/tmp/sample_registry.json",
        all_conflicts=True,
    )
    cases = _map_cases(samples, units)
    service = ConflictRetrievalService(
        repository=repository,
        embedding_client=OpenAICompatibleEmbeddingClient(settings),
        reranker=None,
    )

    rows: list[dict[str, Any]] = []
    for case in cases:
        query_unit = case["query_units"][0]
        result = service.retrieve_for_unit(str(query_unit["id"]), top_k=20)
        ranks = [_rank_for_group(result.results, group) for group in case["target_groups"]]
        finite = [rank for rank in ranks if rank is not None]
        rows.append(
            {
                "sample_id": case["sample_id"],
                "error_type": case["error_type"],
                "query_unit_id": str(query_unit["id"]),
                "target_group_ranks": ranks,
                "best_rank": min(finite) if finite else None,
                "candidate_counts": result.candidate_counts,
                "branch_metadata": result.branch_metadata,
                "skipped_branches": result.skipped_branches,
            }
        )

    summary = {
        "all_70": _summarize(rows),
        "cross_policy_pt_35": _summarize(
            [row for row in rows if row["sample_id"].startswith("PT-")]
        ),
        "same_policy_cf_35": _summarize(
            [row for row in rows if row["sample_id"].startswith("CF-")]
        ),
    }
    report = {
        "benchmark": {
            "cases": len(rows),
            "pipeline": "same_title + leaf_category + parent_category + global",
            "reranker_run": False,
            "returned_candidates": 20,
            "source_policy_excluded": True,
            "notes": [
                "PT cases place target evidence in other policies and measure this module's scope.",
                "CF cases place query and target evidence in one policy; exclusion intentionally removes them.",
            ],
        },
        "summary": summary,
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_any_target_recall": {
            f"@{k}": sum(row["best_rank"] is not None and row["best_rank"] <= k for row in rows)
            / len(rows)
            for k in KS
        },
        "case_all_targets_recall": {
            f"@{k}": sum(
                all(rank is not None and rank <= k for rank in row["target_group_ranks"])
                for row in rows
            )
            / len(rows)
            for k in KS
        },
        "misses_at_10": [
            row["sample_id"]
            for row in rows
            if row["best_rank"] is None or row["best_rank"] > 10
        ],
        "all_target_misses_at_10": [
            row["sample_id"]
            for row in rows
            if any(rank is None or rank > 10 for rank in row["target_group_ranks"])
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the reusable Proof conflict retrieval pipeline.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/conflict-method-eval/conflict-pipeline-benchmark.json"),
    )
    args = parser.parse_args()
    print(json.dumps(run(args.output)["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
