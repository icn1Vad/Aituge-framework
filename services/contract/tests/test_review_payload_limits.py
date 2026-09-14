"""Review payload size is telemetry, not grounds to discard valid evidence.

All completions below are local fakes; no provider or embedding calls.
"""
import ast
import asyncio
from dataclasses import replace
import importlib
import inspect
import json
from pathlib import Path

import pytest
from contract.risk.plan_builder import RiskReviewPlanBuilder
from risk_test_data import risk_plan_input
from services.contract.capabilities import risk_review as cf, risk_review_bundle as base
from services.contract.capabilities import horizontal_review, finding_consolidation
from test_contract_risk_direct_review import _request, _valid_payload, _completion, FakeRuntime
from test_contract_risk_base_bundle import _po_request, _po_candidate_payload, _completion as po_completion
from test_legal_evidence_binding import _bundle
from contract.legal_evidence.prompting import compact_legal_evidence_catalog
from contract.rule_evidence.reviewer import RuleLibraryReviewer
from contract.rule_evidence.shadow import RuleLibraryShadow
from test_rule_library_reviewer import FakeModel
from test_rule_library_shadow import snapshot

INVENTORY = json.loads((Path(__file__).parent / 'fixtures/review_payload_limit_inventory.json').read_text())


@pytest.mark.parametrize('entry', INVENTORY, ids=lambda x: x['field'])
def test_removed_schema_quota_cannot_reappear(entry):
    path = entry['path'].removesuffix('.py')
    module = path.removeprefix('services/contract/src/').replace('/', '.') if '/src/' in path else path.replace('/', '.')
    class_name, field_name = entry['field'].split('.')
    model = getattr(importlib.import_module(module), class_name)
    field = model.model_fields[field_name]
    assert not any(getattr(meta, 'max_length', None) is not None for meta in field.metadata)


@pytest.mark.parametrize('estimate', [6060, 24001, 100001])
@pytest.mark.parametrize('domain', base.BASE_UNIT_IDS)
def test_all_real_request_factories_accept_large_estimates(domain, estimate):
    context = next(x for x in RiskReviewPlanBuilder().build(risk_plan_input()).contexts if x.unit_id == domain)
    payload = context.model_dump(mode='json')
    payload['estimated_input_tokens'] = estimate
    if domain == 'commercial_financial':
        request = cf.commercial_request_from_context(payload)
    else:
        request = base.generic_request_from_context(payload, allow_absence_only_evidence_catalog=True)
    assert request.estimated_input_tokens == estimate


def test_commercial_large_completion_and_long_reason_survive_materialization():
    payload = _valid_payload()
    finding = payload['check_results'][0]['findings'][0]
    finding['issue'] = '价款约定需要进一步明确。' * 900
    finding['suggestion'] = '明确总价及计价范围。' * 900
    payload['check_results'][0]['decision_note'] = '存在有原文依据的价款风险。' * 200
    completion = replace(_completion(json.dumps(payload, ensure_ascii=False)),
        completion_tokens=9001, prompt_tokens=18000, total_tokens=27001)
    runtime = FakeRuntime([completion])
    result = asyncio.run(cf.CommercialFinancialDirectReviewer(runtime_factory=lambda _:runtime).review(
        _request(), tenant_id='offline', model_id='fake'))
    assert result.findings[0].issue == finding['issue']
    assert result.findings[0].suggestion == finding['suggestion']
    assert result.completion_tokens == 9001 and result.repair_count == 0
    assert runtime.calls[0]['max_tokens'] is None


def test_candidate_output_larger_than_4000_is_not_rejected():
    request = _po_request()
    raw = _po_candidate_payload(request, risk_check_code='PO-001', risk_candidate_type='SCOPE_EXPANSION')
    completion = replace(po_completion(json.dumps(raw, ensure_ascii=False),request.unit_id),
        completion_tokens=9001, total_tokens=20000)
    runtime = FakeRuntime([completion])
    result = asyncio.run(base.GenericBaseDirectReviewer(runtime_factory=lambda _:runtime).review(
        request, tenant_id='offline', model_id='fake'))
    assert result.status == 'COMPLETED' and result.repair_count == 0
    assert runtime.calls[0]['max_tokens'] is None


@pytest.mark.parametrize('budget', [0, 1, 20, 1800])
def test_legacy_legal_catalog_cannot_omit_or_clip_text(budget):
    text = '完整法条正文；' * 2000 + '但符合本条例外的，不适用前款。'
    evidence = _bundle(text, domain='commercial_financial',check_codes=['CF-001']).evidence
    catalog, count = compact_legal_evidence_catalog(evidence, maximum_catalog_tokens=budget, maximum_excerpt_characters=1)
    assert catalog[0]['content_excerpt'] == text and not catalog[0]['content_truncated']
    assert count > budget


def test_consolidation_preserves_all_text_and_more_than_eight_evidence_items():
    text = '前文依据。' * 1000 + '条末限定与例外。'
    finding = {'title':text, 'issue':text, 'evidence_candidates':[
        {'evidence_type':'TEXT_QUOTE','quoted_text':text,'source_ir_item_id':str(i),
         'block_id':str(i),'char_start':0,'char_end':len(text)} for i in range(21)]}
    summary = finding_consolidation._finding_summary(finding, finding['evidence_candidates'])
    assert summary['title'] == text and summary['issue'] == text
    assert len(summary['evidence']) == 21
    assert all(item['quoted_text'] == text for item in summary['evidence'])


@pytest.mark.parametrize('module', [cf,base,horizontal_review,finding_consolidation])
def test_no_review_specific_output_ceiling_or_numeric_rejection_gate(module):
    tree=ast.parse(inspect.getsource(module))
    forbidden={'RISK_OUTPUT_BUDGET_EXCEEDED','RISK_CONTEXT_BUDGET_EXCEEDED'}
    assert not {n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)} & forbidden
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and getattr(node.func,'attr',None)=='complete_with_usage':
            assert all(isinstance(k.value,ast.Constant) and k.value.value is None
                for k in node.keywords if k.arg=='max_tokens')


def test_oversized_rule_is_attempted_with_full_text_and_without_paid_retry(tmp_path):
    plan = RiskReviewPlanBuilder().build(risk_plan_input())
    observation = RuleLibraryShadow(snapshot(tmp_path)).evaluate(
        plan,tenant_id='42',contract_type_name='采购合同',business_role='买受方')
    # Preserve snapshot identity: large source contexts, not made-up rule identities.
    text = '该条付款条件依验收记录认定。' * 3000
    for context in plan.contexts:
        for source in context.evidence_sources:
            source.quoted_text = text
    runtime=FakeModel()
    result=asyncio.run(RuleLibraryReviewer(runtime,max_prompt_chars=2000).review(
        observation,plan,tenant_id='42',model_id='fake'))
    assert runtime.calls == 1 and result.status == 'COMPLETED'
    assert result.decisions[0].citations[0].quoted_text == text
    assert not result.pending_evidence_ids
