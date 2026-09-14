"""Always-run IR -> plan -> request -> prompt tests; no external fixture/model."""
import ast
import inspect
import json

import pytest
from contract.risk.plan_builder import RiskReviewPlanBuilder
from risk_test_data import risk_plan_input
from test_legal_evidence_binding import _bundle
from services.contract.capabilities import risk_review, risk_review_bundle, horizontal_review


@pytest.mark.parametrize("with_law", [False, True])
@pytest.mark.parametrize("dictionary_transport", [False, True])
@pytest.mark.parametrize("domain", risk_review_bundle.BASE_UNIT_IDS)
def test_plan_context_enters_real_request_factory_and_prompt(domain, with_law, dictionary_transport):
    plan = RiskReviewPlanBuilder().build(risk_plan_input())
    contexts = [context for context in plan.contexts if context.unit_id == domain]
    assert contexts, domain
    for context in contexts:
        content = "条文正文；" * 2000 + "但满足例外情形时不适用。"
        laws = _bundle(content, domain=domain, check_codes=[context.check_specs[0].check_code]).evidence if with_law else []
        factory, prompt = ((risk_review.commercial_request_from_context, risk_review._prompt)
            if domain == "commercial_financial" else (risk_review_bundle.generic_request_from_context, risk_review_bundle._generic_prompt))
        # Formal review enables dynamic batching, including valid absence-only contexts.
        kwargs = {} if domain == "commercial_financial" else {"allow_absence_only_evidence_catalog": True}
        request = factory(context.model_dump(mode="json") if dictionary_transport else context, legal_evidence=laws, **kwargs)
        payload = json.loads(prompt(request)[0].split("\n", 1)[1])
        assert request.batch_id == context.batch_id
        assert [spec.check_code for spec in request.assigned_check_specs] == [spec.check_code for spec in context.check_specs]
        if with_law:
            assert request.legal_evidence_prompt_status == "INCLUDED"
            assert payload["legal_evidence_catalog"][0]["content_excerpt"] == content
            assert not payload["legal_evidence_catalog"][0]["content_truncated"]
        else:
            assert request.legal_evidence_prompt_status == "NOT_REQUESTED"
            assert "legal_evidence_catalog" not in payload


@pytest.mark.parametrize("module", [risk_review, risk_review_bundle, horizontal_review])
def test_review_entrypoints_do_not_load_retired_legal_budget_helpers(module):
    source = ast.parse(inspect.getsource(module))
    retired = {"compact_legal_evidence_catalog", "remaining_legal_prompt_budget", "legal_evidence_ids_for_check"}
    names = {node.id for node in ast.walk(source) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    assert not (names & retired)
