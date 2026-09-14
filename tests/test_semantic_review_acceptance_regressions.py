"""Offline regressions derived from the five-file live acceptance, plus negative controls."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from services.contract.capabilities import horizontal_review as hz, risk_review as cf
from test_seven_domain_evidence_protocol import full_payment_request, horizontal_candidate, Runtime
from risk_test_data import risk_plan_input


@pytest.mark.parametrize('timing',[
    '验收合格后','完成验收工作30个工作日内','完成验收工作12个月后','完成验收工作24个月后',
    '验收合格之日起十日内','交付完成后','通过验收后','履约完成后',
])
@pytest.mark.parametrize('side',['PARTY_A','PARTY_B'])
def test_actual_post_acceptance_payment_survives_noise(timing,side):
    text='本合同款项按照以下方式支付。'+timing+'，甲方支付全部价款。'
    text+='本合同以人民币付款（单位：元）。伴随服务费用已包含在合同价中，甲方不再另行支付。'
    request=full_payment_request(text)
    if side=='PARTY_B':
        request=request.model_copy(update={'perspective':side,'our_party':request.counterparty,'counterparty':request.our_party})
    candidate=cf._prompt(request)[3]
    assert len(candidate.payment_events)==1
    assert candidate.trigger_absence_verified and not candidate.payment_before_performance
    assert candidate.payer_role_status==('OUR_PARTY' if side=='PARTY_A' else 'COUNTERPARTY')


@pytest.mark.parametrize('text',[
    '验收合格前甲方支付全部价款。','甲方收到发票后支付全部价款。',
    '甲方支付全部价款。','验收合格后甲方支付80%，剩余20%另行支付。',
    '合同签订后预付25%价款。验收合格后甲方支付75%价款。',
])
def test_unknown_or_advance_payment_never_becomes_verified_after(text):
    candidate=cf._prompt(full_payment_request(text))[3]
    assert candidate.payment_events and not candidate.trigger_absence_verified


def row(candidate,verdict='RISK'):
    return dict(candidate_id=candidate.candidate_id,verdict=verdict,decision_summary='根据提供的记录判断',
        primary_evidence_source_ids=['left','right'] if candidate.unit_id.startswith('cross') else ['left','absent'],
        recommended_control_codes=['CLARIFY'] if verdict=='RISK' else [],
        resolution_reason='原文已消除候选问题' if verdict=='NO_RISK' else None)


def test_no_risk_existing_explanation_reused_without_new_judgment():
    candidate=horizontal_candidate()
    raw=row(candidate,'NO_RISK'); raw.pop('decision_summary')
    valid,errors=hz._recover_horizontal_rows(json.dumps({'candidate_decisions':[raw]}),[candidate])
    assert not errors and valid[candidate.candidate_id].decision_summary==raw['resolution_reason']


@pytest.mark.parametrize('bad',['no_summary','no_absence','unknown_source','legal_id','wrong_scope','empty_json','duplicate'])
def test_horizontal_errors_get_one_targeted_repair_with_complete_sources(monkeypatch,bad):
    candidate=horizontal_candidate('missing_ambiguity_completeness')
    good=row(candidate); broken=copy.deepcopy(good)
    if bad=='no_summary': broken.pop('decision_summary')
    elif bad=='no_absence': broken['primary_evidence_source_ids']=['context']
    elif bad=='unknown_source': broken['primary_evidence_source_ids']=['missing']
    elif bad=='legal_id': broken['primary_evidence_source_ids']=['legal-evidence-123']
    elif bad=='wrong_scope': broken['primary_evidence_source_ids']=['someone-elses-absence']
    initial={'candidate_decisions':[broken,broken] if bad=='duplicate' else [broken]}
    if bad=='empty_json': initial={}
    runtime=Runtime(initial,{'candidate_decisions':[good]})
    batch=hz.HorizontalBatch(batch_id='risk-batch-'+'2'*32,unit_id=candidate.unit_id,
        check_codes=[candidate.check_code],candidate_ids=[candidate.candidate_id],
        evidence_source_ids=['left','context'],estimated_business_context_tokens=50)
    source=hz.HorizontalEvidenceSource.model_construct(source_id='left',quoted_text='见附件一',block_id='b1')
    context=hz.HorizontalEvidenceSource.model_construct(source_id='context',quoted_text='相关背景',block_id='b2')
    absence=NS(source_id='absent',model_dump=lambda **_:dict(source_id='absent',checked_scope='全部当前合同',verification_method='未找到附件一正文'))
    plan=NS(candidates=[candidate],batches=[batch],check_codes=[candidate.check_code],evidence_sources=[source,context],absence_evidence_sources=[absence])
    monkeypatch.setattr(hz,'_materialize_unit',lambda **kwargs:NS(**kwargs))
    result=asyncio.run(hz.execute_horizontal_unit(risk_plan_input(),plan,candidate.unit_id,
        tenant_id='test',model_id='fake',runtime_factory=lambda _:runtime))
    assert len(runtime.calls)==2 and result.metrics[0].repair_count==1
    assert result.metrics[0].status=='COMPLETED' and result.decisions[0].verdict=='RISK', result.metrics[0]
    repair=json.loads(runtime.calls[1]['messages'][0]['content'])
    assert repair['evidence_sources'] and repair['absence_evidence_sources']
    assert repair['repair']['errors'] and 'response_schema' in repair['output_contract']
    assert result.metrics[0].total_tokens==6800


def test_horizontal_valid_sibling_is_not_repaired_or_silently_changed():
    first=horizontal_candidate(); second=first.model_copy(update={'candidate_id':'horizontal-candidate-'+'2'*32})
    good=row(first); bad=row(second); bad['primary_evidence_source_ids']=['invented']
    valid,errors=hz._recover_horizontal_rows(json.dumps({'candidate_decisions':[good,bad]}),[first,second])
    assert set(valid)=={first.candidate_id}
    assert [e['candidate_id'] for e in errors]==[second.candidate_id]


def test_legacy_attachment_context_is_valid_trigger_but_not_unrelated_support():
    candidate=horizontal_candidate('missing_ambiguity_completeness').model_copy(update={
        'left_evidence_source_ids':[], 'context_evidence_source_ids':['context'],
        'allowed_supporting_evidence_source_ids':['unrelated']})
    raw=row(candidate); raw['primary_evidence_source_ids']=['context','absent']
    valid,errors=hz._recover_horizontal_rows(json.dumps({'candidate_decisions':[raw]}),[candidate])
    assert not errors and valid[candidate.candidate_id].verdict=='RISK'
    raw['primary_evidence_source_ids']=['unrelated','absent']
    valid,errors=hz._recover_horizontal_rows(json.dumps({'candidate_decisions':[raw]}),[candidate])
    assert errors and not valid


def test_final_cf_wire_contract_does_not_contradict_required_evidence_fields():
    prompt=cf._prompt(full_payment_request('验收合格后甲方付清全部价款。'))[0]
    payload=json.loads(prompt.split('\n',1)[1])
    contract=payload['output_contract']
    assert 'decision_evidence_source_ids' in contract['conditional_required_check_fields']['required']
    assert all('每项只允许check_code,status' not in rule for rule in contract['rules'])
    assert 'candidate_evidence_source_ids' in contract['cf005_required_fields']
