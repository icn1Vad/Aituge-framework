from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SRC = ROOT / "services" / "contract" / "src"
if str(CONTRACT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTRACT_SRC))

from services.contract.capabilities.prompt_budget import evaluate_prompt_budget
from services.contract.scripts.contract_risk_prompt_budget_replay import (
    replay_prompt_budget_artifact,
)


BATCH_ID = "risk-batch-" + "1" * 32


@pytest.mark.parametrize(
    ("provider_prompt_tokens", "expected"),
    (
        (5999, "WITHIN_TARGET"),
        (6000, "WITHIN_TARGET"),
        (6001, "SOFT_WARNING"),
        (6145, "SOFT_WARNING"),
        (7000, "SOFT_WARNING"),
        (7001, "HARD_LIMIT_EXCEEDED"),
    ),
)
def test_provider_prompt_budget_boundaries(
    provider_prompt_tokens: int,
    expected: str,
) -> None:
    value = evaluate_prompt_budget(
        unit_id="formation_validity_authority",
        batch_id=BATCH_ID,
        estimated_business_context_tokens=9000,
        client_estimated_prompt_tokens=8000,
        client_tokenizer_name="Qwen3-32B-Tokenizer",
        provider_prompt_tokens=provider_prompt_tokens,
        provider_cached_tokens=min(provider_prompt_tokens, 4096),
    )

    assert value.policy_version == "2.0"
    assert value.budget_status == expected
    assert value.provider_prompt_tokens == provider_prompt_tokens
    assert value.estimated_business_context_tokens == 9000
    assert value.client_estimated_prompt_tokens == 8000


def test_fva_6145_is_a_non_blocking_soft_warning() -> None:
    value = evaluate_prompt_budget(
        unit_id="formation_validity_authority",
        batch_id=BATCH_ID,
        estimated_business_context_tokens=5419,
        provider_prompt_tokens=6145,
        provider_cached_tokens=6144,
    )

    assert value.budget_status == "SOFT_WARNING"
    assert value.tokens_over_target == 145
    assert value.tokens_over_hard_limit == 0
    assert value.over_target_ratio == pytest.approx(0.024167)


def test_cached_tokens_are_a_prompt_subset_not_an_additional_budget() -> None:
    value = evaluate_prompt_budget(
        unit_id="formation_validity_authority",
        batch_id=BATCH_ID,
        estimated_business_context_tokens=5419,
        provider_prompt_tokens=6000,
        provider_cached_tokens=6000,
    )

    assert value.budget_status == "WITHIN_TARGET"
    assert value.provider_prompt_tokens == 6000


def test_invalid_cached_tokens_are_rejected() -> None:
    with pytest.raises(ValueError, match="subset"):
        evaluate_prompt_budget(
            unit_id="formation_validity_authority",
            batch_id=BATCH_ID,
            estimated_business_context_tokens=5419,
            provider_prompt_tokens=6000,
            provider_cached_tokens=6001,
        )


def test_missing_provider_usage_is_not_filled_from_client_estimate() -> None:
    value = evaluate_prompt_budget(
        unit_id="formation_validity_authority",
        batch_id=BATCH_ID,
        estimated_business_context_tokens=5419,
        client_estimated_prompt_tokens=7212,
        client_tokenizer_name="Qwen3-32B-Tokenizer",
        provider_prompt_tokens=None,
        provider_cached_tokens=None,
    )

    assert value.budget_status == "PROVIDER_USAGE_UNAVAILABLE"
    assert value.provider_prompt_tokens is None
    assert value.tokens_over_target is None
    assert value.client_estimated_prompt_tokens == 7212


def test_existing_three_run_artifact_replays_one_fva_warning_per_run() -> None:
    provider_tokens = {
        "formation_validity_authority": 6145,
        "commercial_financial": 5677,
        "performance_obligations-a": 5309,
        "performance_obligations-b": 4447,
        "ip_confidentiality_data": 2525,
        "liability_remedies_exit-a": 2699,
        "liability_remedies_exit-b": 5561,
    }
    bundles = []
    for run_index in range(3):
        metrics = []
        for index, (unit_id, prompt_tokens) in enumerate(
            provider_tokens.items(),
            1,
        ):
            normalized_unit = unit_id.removesuffix("-a").removesuffix("-b")
            metrics.append(
                {
                    "unit_id": normalized_unit,
                    "batch_id": f"risk-batch-{index:032x}",
                    "prompt_tokens": prompt_tokens,
                    "cached_tokens": min(prompt_tokens, 4096),
                }
            )
        bundles.append({"run_index": run_index + 1, "batch_execution_metrics": metrics})

    result = replay_prompt_budget_artifact(
        {
            "artifact_type": "CONTRACT_RISK_STAGE63_BASE_BUNDLE_V1",
            "bundles": bundles,
        }
    )

    assert result["status"] == "PASSED"
    assert result["model_call_count"] == 0
    assert len(result["runs"]) == 3
    assert all(
        item["prompt_budget_warning_count"] == 1
        and item["prompt_budget_hard_failure_count"] == 0
        and item["max_provider_prompt_tokens"] == 6145
        and len(item["batches_over_target"]) == 1
        and item["batches_over_hard_limit"] == []
        for item in result["runs"]
    )
