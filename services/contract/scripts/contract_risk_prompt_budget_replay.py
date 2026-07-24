#!/usr/bin/env python3
"""Replay Stage 6.3 Provider prompt-token budgets without calling a model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_SRC = ROOT / "services" / "contract" / "src"
for path in (ROOT, CONTRACT_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from services.contract.capabilities.prompt_budget import (  # noqa: E402
    PROMPT_BUDGET_POLICY_VERSION,
    evaluate_prompt_budget,
)


def replay_prompt_budget_artifact(value: dict[str, Any]) -> dict[str, Any]:
    runs = []
    for run_index, bundle in enumerate(value.get("bundles") or [], 1):
        batches = []
        for metric in bundle.get("batch_execution_metrics") or []:
            budget = evaluate_prompt_budget(
                unit_id=metric["unit_id"],
                batch_id=metric["batch_id"],
                estimated_business_context_tokens=None,
                provider_prompt_tokens=metric.get("prompt_tokens"),
                provider_cached_tokens=metric.get("cached_tokens"),
            )
            batches.append(budget.model_dump(mode="json"))
        hard_failures = [
            item for item in batches
            if item["budget_status"] == "HARD_LIMIT_EXCEEDED"
        ]
        warnings = [
            item for item in batches if item["budget_status"] == "SOFT_WARNING"
        ]
        provider_values = [
            item["provider_prompt_tokens"]
            for item in batches
            if item["provider_prompt_tokens"] is not None
        ]
        runs.append(
            {
                "run_index": run_index,
                "budget_status": "FAILED" if hard_failures else "PASSED",
                "prompt_budget_warning_count": len(warnings),
                "prompt_budget_hard_failure_count": len(hard_failures),
                "max_provider_prompt_tokens": (
                    max(provider_values) if provider_values else None
                ),
                "batches_over_target": [
                    item["batch_id"] for item in warnings
                ],
                "batches_over_hard_limit": [
                    item["batch_id"] for item in hard_failures
                ],
                "batches": batches,
            }
        )
    return {
        "artifact_type": "CONTRACT_RISK_PROMPT_BUDGET_REPLAY_V2",
        "policy_version": PROMPT_BUDGET_POLICY_VERSION,
        "source_artifact_type": value.get("artifact_type"),
        "status": (
            "PASSED"
            if runs
            and all(item["budget_status"] == "PASSED" for item in runs)
            else "FAILED"
        ),
        "model_call_count": 0,
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = replay_prompt_budget_artifact(
        json.loads(args.input.read_text(encoding="utf-8"))
    )
    payload = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output is None:
        print(payload)
    else:
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
