from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SRC = ROOT / "services" / "contract" / "src"
CONTRACT_TESTS = ROOT / "services" / "contract" / "tests"
for path in (CONTRACT_SRC, CONTRACT_TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from contract.risk.plan_builder import RiskReviewPlanBuilder
from common.tokenization import estimate_tokens_in_text
from risk_fixture_loader import load_fixed_risk_plan_input
from services.contract.capabilities.risk_review import (
    _SYSTEM_PROMPT,
    _prompt,
    commercial_request_from_context,
)


FIXTURE_ENV = "CONTRACT_RISK_FIXTURE_DIR"


@pytest.mark.skipif(not os.getenv(FIXTURE_ENV), reason=f"{FIXTURE_ENV} is not configured")
def test_fixed_fixture_builds_one_strict_commercial_direct_request_without_running_ir() -> None:
    value = load_fixed_risk_plan_input(Path(os.environ[FIXTURE_ENV]))
    semantic = value.stage_result.semantic_ir.model_dump(mode="json")
    assert sum(len(items) for items in semantic.values()) == 101
    assert len(value.source_blocks) == 93

    plan = RiskReviewPlanBuilder().build(value)
    unit = next(item for item in plan.review_units if item.unit_id == "commercial_financial")
    contexts = [item for item in plan.contexts if item.unit_id == "commercial_financial"]

    assert len(unit.batch_ids) == 1
    assert len(contexts) == 1
    request = commercial_request_from_context(contexts[0])
    assert [item.check_code for item in request.assigned_check_specs] == [
        f"CF-{index:03d}" for index in range(1, 9)
    ]
    assert request.estimated_input_tokens <= 6000
    assert request.projected_ir_items
    assert request.source_excerpts
    assert all(
        excerpt.quoted_text_hash.startswith("sha256:")
        for excerpt in request.source_excerpts
    )
    prompt, _ir_refs, _anchor_refs, candidate = _prompt(request)
    prompt_tokens = estimate_tokens_in_text(_SYSTEM_PROMPT) + estimate_tokens_in_text(prompt)
    assert prompt_tokens <= 6000
    assert candidate.check_code == "CF-005"
    assert candidate.substantial_prepayment is True
    assert candidate.payment_before_performance is True
    assert candidate.identified_security_mechanisms == []
    assert candidate.candidate_ir_refs
    assert candidate.candidate_evidence_refs
