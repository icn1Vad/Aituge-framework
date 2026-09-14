"""Live wire-contract boundaries with fake completions; no provider calls."""
import asyncio
import copy
import json
import pytest

from contract.legal_evidence.prompting import selected_legal_evidence_ids, merge_legal_reasoning
from services.contract.capabilities import risk_review as cf, risk_review_bundle as base, horizontal_review as hz
from services.contract.capabilities.review_evidence_protocol import direct_wire_schema
from test_seven_domain_evidence_protocol import Runtime, wire_fixture, catalog_for, horizontal_candidate
from test_contract_risk_direct_review import _request as cf_request, _valid_payload
from test_contract_risk_base_bundle import _request as fva_request, _payload as fva_payload, _po_request, _po_candidate_payload
from test_legal_evidence_binding import _bundle


def law(check='CF-001', domain='commercial_financial'):
    return _bundle('完整法律正文；不得省略条末例外。', domain=domain, check_codes=[check]).evidence[0]


def test_law_binding_uses_explicit_frozen_id_not_prose_or_retrieval_tag():
    item = law()
    assert selected_legal_evidence_ids([item], [item.evidence_id], prompt_included=True) == [item.evidence_id]
    assert merge_legal_reasoning('名称可使用简称。', [], [item], 'CF-008', prompt_included=True,
        selected_ids=[item.evidence_id]).endswith('名称可使用简称。')
    assert merge_legal_reasoning('《测试法规》第1条', [], [item], 'CF-001', prompt_included=True,
        selected_ids=[]) == '《测试法规》第1条'
    for ids, included in [(['foreign-law'], True), ([item.evidence_id], False), ([item.evidence_id]*2, True)]:
        with pytest.raises(ValueError):
            selected_legal_evidence_ids([item], ids, prompt_included=included)


@pytest.mark.parametrize('factory,payload_factory,reviewer', [
    (cf_request, _valid_payload, cf.CommercialFinancialDirectReviewer),
    (fva_request, fva_payload, base.GenericBaseDirectReviewer),
])
def test_actual_direct_review_binds_paraphrased_law_by_id(factory,payload_factory,reviewer):
    request = factory()
    raw = payload_factory()
    check = next(c for c in raw['check_results'] if c['findings'])
    item = law(check['check_code'], request.unit_id)
    request.legal_evidence = [item]
    target = check['findings'][0]
    target['legal_evidence_ids'] = [item.evidence_id]
    # Deliberately no exact citation label: association must not depend on regex.
    target['issue'] += ' 所选法律依据支持这一审查判断，适用性待核验。'
    runtime = Runtime(wire_fixture(raw, catalog_for(request)))
    result = asyncio.run(reviewer(runtime_factory=lambda _:runtime).review(request, tenant_id='test', model_id='fake'))
    assert result.model_call_count == 1
    assert any(f.legal_evidence_ids == [item.evidence_id] for f in result.findings)
    prompt = json.loads(runtime.calls[0]['messages'][0]['content'].split('\n',1)[1])
    assert prompt['legal_evidence_catalog'][0]['evidence_id'] == item.evidence_id
    assert prompt['legal_evidence_catalog'][0]['content_excerpt'] == item.unit.content


def test_unknown_law_id_repaired_without_prose_guessing_or_lost_catalog():
    request = cf_request()
    item = law()
    request.legal_evidence = [item]
    good = wire_fixture(_valid_payload(), catalog_for(request))
    good['check_results'][0]['findings'][0]['legal_evidence_ids'] = [item.evidence_id]
    bad = copy.deepcopy(good)
    bad['check_results'][0]['findings'][0]['legal_evidence_ids'] = ['unknown-law']
    runtime = Runtime(bad, {'check_results':[good['check_results'][0]]})
    result = asyncio.run(cf.CommercialFinancialDirectReviewer(runtime_factory=lambda _:runtime).review(request, tenant_id='test',model_id='fake'))
    assert result.model_call_count == 2
    assert result.findings[0].legal_evidence_ids == [item.evidence_id]
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert repair['target_check_codes'] == ['CF-001']
    assert repair['context']['legal_evidence_catalog'][0]['evidence_id'] == item.evidence_id


@pytest.mark.parametrize('schema', [cf.ModelFindingDraft, base.GenericModelFindingDraft,
    base.CandidateDecisionRaw, hz.HorizontalDecisionRaw])
def test_model_schemas_select_law_ids_without_technical_evidence_fields(schema):
    wire = direct_wire_schema(schema.model_json_schema())
    assert wire['properties']['legal_evidence_ids']['type'] == 'array'
    assert not ({'quoted_text','quoted_text_hash','char_start','char_end','page_number','block_id','evidence'} & set(wire['properties']))


@pytest.mark.parametrize('unit', hz.HORIZONTAL_UNIT_IDS)
def test_horizontal_bad_law_is_isolated_before_materialization(unit):
    first = horizontal_candidate(unit)
    second = first.model_copy(update={'candidate_id':'horizontal-candidate-'+'2'*32})
    item = law(first.check_code, unit)
    ids = ['left','right'] if unit.startswith('cross') else ['left','absent']
    good = dict(candidate_id=first.candidate_id,verdict='RISK',decision_summary='原文存在冲突或引用缺失，适用所选法条。',
        primary_evidence_source_ids=ids,legal_evidence_ids=[item.evidence_id],recommended_control_codes=['CLARIFY'])
    bad = {**good,'candidate_id':second.candidate_id,'legal_evidence_ids':['foreign-law']}
    valid, errors = hz._recover_horizontal_rows(json.dumps({'candidate_decisions':[good,bad]}),[first,second],[item])
    assert list(valid) == [first.candidate_id]
    assert valid[first.candidate_id].legal_evidence_ids == [item.evidence_id]
    assert len(errors)==1 and errors[0]['candidate_id']==second.candidate_id


def test_candidate_law_selection_survives_canonical_finding_materialization():
    request = _po_request()
    request.legal_evidence = [law('PO-004', request.unit_id)]
    raw = _po_candidate_payload(request, risk_check_code='PO-004')
    for decision in raw['candidate_decisions']:
        if decision['verdict']=='RISK':
            decision['legal_evidence_ids'] = [request.legal_evidence[0].evidence_id]
    runtime = Runtime(raw)
    result = asyncio.run(base.GenericBaseDirectReviewer(runtime_factory=lambda _:runtime).review(request,tenant_id='test',model_id='fake'))
    assert result.findings
    assert any(f.legal_evidence_ids == [request.legal_evidence[0].evidence_id] for f in result.findings)
