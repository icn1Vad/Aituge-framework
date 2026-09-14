"""Raw wire regressions: deliberately NO wire_fixture/default-field migration."""
import asyncio
import copy
import json

import pytest
from services.contract.capabilities import risk_review as cf, risk_review_bundle as base
from services.contract.capabilities.review_evidence_protocol import DirectSourceCatalog, direct_wire_schema
from test_seven_domain_evidence_protocol import Runtime, cf_request, fva_request


def setup(domain='CF', codes=None):
    request = cf_request() if domain == 'CF' else fva_request()
    codes = codes or (['CF-001', 'CF-002'] if domain == 'CF' else ['FVA-001', 'FVA-005'])
    request = request.model_copy(update={'assigned_check_specs': [s for s in request.assigned_check_specs if s.check_code in codes],
        'check_task_scopes': [s for s in request.check_task_scopes if s.check_code in codes]})
    parts = cf._prompt(request) if domain == 'CF' else base._generic_prompt(request)
    cat = DirectSourceCatalog.build(request, parts[1], parts[2])
    checks = []
    for spec in request.assigned_check_specs:
        checks.append(dict(check_code=spec.check_code, status='REVIEWED', decision_note='已核对所列原文的约定，未见本项实质风险。',
            decision_evidence_source_ids=[sorted(cat.allowed[spec.check_code])[0]], findings=[]))
    return request, parts, cat, {'check_results': checks}


def finding(spec, sid):
    # No nested check_code: it belongs to the enclosing check, not the model.
    return dict(category=spec.allowed_categories[0], risk_type=spec.allowed_risk_types[0], risk_level='MEDIUM',
        title='约定范围不清', issue='原文的义务范围需要进一步明确。', impact_to_our_party='可能产生履行争议。',
        suggestion='明确具体义务范围。', primary_evidence_source_ids=[sid])


def run(request, *raw):
    runtime = Runtime(*raw)
    reviewer = cf.CommercialFinancialDirectReviewer if request.unit_id == cf.COMMERCIAL_UNIT_ID else base.GenericBaseDirectReviewer
    result = asyncio.run(reviewer(runtime_factory=lambda _: runtime).review(request, tenant_id='offline', model_id='fake'))
    return result, runtime


@pytest.mark.parametrize('domain', ['CF', 'FVA'])
def test_raw_missing_nested_owner_is_bound_without_model_retry(domain):
    req, _, cat, raw = setup(domain)
    row = raw['check_results'][0]
    row['findings'] = [finding(req.assigned_check_specs[0], sorted(cat.allowed[row['check_code']])[0])]
    frozen = copy.deepcopy(raw)
    result, runtime = run(req, raw)
    assert result.status == 'COMPLETED' and len(runtime.calls) == 1
    assert result.findings[0].check_code == row['check_code']
    assert result.findings[0].evidence_candidates[0].anchor_id
    assert raw == frozen  # Caller/raw diagnostic never rewritten.


@pytest.mark.parametrize('domain', ['CF', 'FVA'])
@pytest.mark.parametrize('second_error', ['schema', 'business'])
def test_independent_source_and_later_layer_errors_share_one_repair(domain, second_error):
    req, _, cat, good = setup(domain)
    first, second = good['check_results']
    second['findings'] = [finding(req.assigned_check_specs[1], sorted(cat.allowed[second['check_code']])[0])]
    bad = copy.deepcopy(good)
    bad['check_results'][0].pop('decision_evidence_source_ids')
    if second_error == 'schema':
        bad['check_results'][1]['findings'][0].pop('risk_level')
    else:
        bad['check_results'][1]['findings'][0]['risk_type'] = 'NOT_AN_ASSIGNED_RISK'
    result, runtime = run(req, bad, good)
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert result.status == 'COMPLETED' and len(runtime.calls) == 2
    assert set(repair['target_check_codes']) == {first['check_code'], second['check_code']}
    assert {i['check_code'] for i in repair['errors']} == {first['check_code'], second['check_code']}
    assert any(i['stage'] == 'EVIDENCE' for i in repair['errors'])
    assert any(i['stage'] == second_error.upper() for i in repair['errors'])


@pytest.mark.parametrize('domain', ['CF', 'FVA'])
def test_explicit_conflicting_owner_is_not_silently_rebound(domain):
    req, _, cat, good = setup(domain)
    row = good['check_results'][0]
    row['findings'] = [finding(req.assigned_check_specs[0], sorted(cat.allowed[row['check_code']])[0])]
    bad = copy.deepcopy(good)
    bad['check_results'][0]['findings'][0]['check_code'] = good['check_results'][1]['check_code']
    result, runtime = run(req, bad, {'check_results': [row]})
    assert result.status == 'COMPLETED' and len(runtime.calls) == 2
    assert json.loads(runtime.calls[1]['messages'][0]['content'])['target_check_codes'] == [row['check_code']]


def test_delivery_prompt_declares_status_and_negative_basis_without_prepayment_check():
    req, parts, cat, good = setup(codes=['CF-007'])
    body = json.loads(parts[0].split('\n', 1)[1])
    contract = body['output_contract']
    assert contract['required_check_fields'] == ['check_code', 'status', 'decision_note', 'findings']
    assert 'status_enum' in contract and 'check_status_enum' not in contract
    assert contract['conditional_required_check_fields']['when']['check_codes'] == ['CF-007']
    assert 'cf005_candidate' not in body
    # Same failure shape as the recorded delivery reply. Mentioning a Source in
    # prose is NOT equivalent to explicitly selecting the judgment basis.
    bad = copy.deepcopy(good)
    row = bad['check_results'][0]
    row['check_status'] = row.pop('status')
    row['decision_note'] += '（' + row.pop('decision_evidence_source_ids')[0] + '）'
    result, runtime = run(req, bad, good)
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert result.status == 'COMPLETED'
    assert [e['path'][-1] for e in repair['errors']] == ['decision_evidence_source_ids']
    assert 'decision_evidence_source_ids' in json.dumps(repair['output_schema'])
    assert 'CF-005' not in json.dumps(repair['context'], ensure_ascii=False)


@pytest.mark.parametrize('domain', ['CF', 'FVA'])
def test_status_alias_is_safe_but_conflicts_require_explicit_repair(domain):
    req, _, _, good = setup(domain)
    aliased = copy.deepcopy(good)
    aliased['check_results'][0]['check_status'] = aliased['check_results'][0].pop('status')
    assert len(run(req, aliased)[1].calls) == 1
    conflict = copy.deepcopy(good)
    conflict['check_results'][0]['check_status'] = 'FAILED'
    result, runtime = run(req, conflict, {'check_results': [good['check_results'][0]]})
    assert result.status == 'COMPLETED' and len(runtime.calls) == 2


@pytest.mark.parametrize('domain', ['CF', 'FVA'])
def test_repair_context_scopes_output_contract_but_preserves_full_sources(domain):
    req, parts, _, good = setup(domain)
    bad = copy.deepcopy(good)
    bad['check_results'][0].pop('decision_evidence_source_ids')
    result, runtime = run(req, bad, {'check_results': [good['check_results'][0]]})
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    context = repair['context'] if domain == 'CF' else json.loads(repair['source_backed_review_context'].split('\n', 1)[1])
    original = json.loads(parts[0].split('\n', 1)[1])
    assert result.status == 'COMPLETED'
    assert context['contract_evidence_catalog'] == original['contract_evidence_catalog']
    assert context['output_contract']['conditional_required_check_fields']['when']['check_codes'] == repair['target_check_codes']


@pytest.mark.parametrize('domain', ['CF', 'FVA'])
@pytest.mark.parametrize('with_sources', [True, False])
def test_wire_schema_declares_required_fields_and_conditional_basis(domain, with_sources):
    req, parts, cat, good = setup(domain)
    schema = cf.ModelCommercialReviewResponseRaw if domain == 'CF' else base.GenericModelResponseRaw
    wire = direct_wire_schema(schema.model_json_schema(), negative_evidence_checks=list(cat.allowed) if with_sources else [])
    definitions = list(wire['$defs'].values())
    check = next(node for node in definitions if 'findings' in node.get('properties', {}))
    item = next(node for node in definitions if 'primary_evidence_source_ids' in node.get('properties', {}))
    assert set(check['required']) >= {'check_code','status','decision_note','findings'}
    assert 'check_code' not in item['properties'] and 'check_code' not in item['required']
    assert ('allOf' in check) is with_sources
    if with_sources:
        rule = check['allOf'][-1]
        assert rule['then']['required'] == ['decision_evidence_source_ids']
        assert rule['then']['properties']['decision_evidence_source_ids']['minItems'] == 1
