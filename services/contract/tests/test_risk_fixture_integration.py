from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import pytest

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.risk.playbooks import BASE_UNIT_IDS, HORIZONTAL_UNIT_IDS

from risk_fixture_loader import load_fixed_risk_plan_input


FIXTURE_ENV = "CONTRACT_RISK_FIXTURE_DIR"


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_fixed_101_item_fixture_builds_deterministic_plan_under_one_second_p95() -> None:
    fixture_dir = Path(os.environ[FIXTURE_ENV])
    value = load_fixed_risk_plan_input(fixture_dir)
    builder = RiskReviewPlanBuilder()

    semantic = value.stage_result.semantic_ir.model_dump(mode="json")
    assert sum(len(items) for items in semantic.values()) == 101
    assert len(value.source_blocks) == 93

    plans = []
    durations = []
    for _ in range(30):
        started = time.perf_counter()
        plans.append(builder.build(value))
        durations.append(time.perf_counter() - started)

    first = plans[0]
    assert all(plan == first for plan in plans[1:])
    assert len(first.review_units) == 7
    assert first.required_unit_ids == list(BASE_UNIT_IDS)
    assert [unit.unit_id for unit in first.review_units[-2:]] == list(HORIZONTAL_UNIT_IDS)
    assert sum(len(unit.check_specs) for unit in first.review_units) == 45
    assert len(first.deterministic_checks) == 11
    assert all(unit.requires_model is False for unit in first.review_units[-2:])
    assert 5 <= len(first.contexts) <= 7
    assert all(
        context.coverage_summary.projected_ir_item_count < 101
        for context in first.contexts
    )
    unit_by_id = {unit.unit_id: unit for unit in first.review_units}
    assert all(
        context.estimated_input_tokens <= unit_by_id[context.unit_id].hard_input_token_limit
        for context in first.contexts
    )

    blocks = {block.block_id: block for block in value.source_blocks}
    for context in first.contexts:
        for excerpt in context.source_excerpts:
            block = blocks[excerpt.block_id]
            assert excerpt.quoted_text == block.text[excerpt.char_start : excerpt.char_end]

    p95 = statistics.quantiles(durations, n=100, method="inclusive")[94]
    assert p95 < 1.0, f"PlanBuilder P95 was {p95:.6f}s"
