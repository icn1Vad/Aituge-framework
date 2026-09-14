"""Audited contradictions converted into positive offline acceptance conditions."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from services.contract.capabilities import risk_review as cf, risk_review_bundle as base, horizontal_review as hz
from services.contract.capabilities.review_evidence_protocol import DirectSourceCatalog, SourceSelectionError
from test_seven_domain_evidence_protocol import (catalog_for, wire_fixture, Runtime, cf_request, fva_request,
    fva_payload, full_payment_request, horizontal_candidate)
from test_contract_risk_direct_review import _valid_payload
from test_contract_risk_base_bundle import _po_request, _icd_request, _po_candidate_payload
from test_legal_evidence_binding import _bundle
from contract.risk.review_ledger import CheckTaskScope


def run_cf(request, *outputs):
    runtime=Runtime(*outputs)
    result=asyncio.run(cf.CommercialFinancialDirectReviewer(runtime_factory=lambda _:runtime).review(
        request,tenant_id='offline',model_id='fake'))
    return result,runtime


def test_all_independent_source_errors_repaired_together_and_siblings_unchanged():
    request=cf_request(); cat=catalog_for(request)
    good=wire_fixture(_valid_payload(),cat); bad=copy.deepcopy(good)
    bad['check_results'][0]['findings'][0]['primary_evidence_source_ids']=['not-a-source']
    bad['check_results'][4]['candidate_evidence_source_ids']=['another-bad-source']
    result,runtime=run_cf(request,bad,{'check_results':[good['check_results'][0],good['check_results'][4]]})
    repair=json.loads(runtime.calls[1]['messages'][0]['content'])
    assert result.status=='COMPLETED' and len(runtime.calls)==2
    assert repair['target_check_codes']==['CF-001','CF-005']
    assert {tuple(i['path']) for i in repair['errors']} == {
        ('check_results',0,'findings',0,'primary_evidence_source_ids'),('check_results',4,'candidate_evidence_source_ids')}
    assert all(i['allowed_source_ids'] and i['rejected_value'] for i in repair['errors'])


@pytest.mark.parametrize('bad_value',[None,1,True,{}])
def test_invalid_findings_shape_goes_through_one_located_repair(bad_value):
    request=cf_request(); good=wire_fixture(_valid_payload(),catalog_for(request)); bad=copy.deepcopy(good)
    bad['check_results'][0]['findings']=bad_value
    result,runtime=run_cf(request,bad,{'check_results':[good['check_results'][0]]})
    assert result.status=='COMPLETED' and len(runtime.calls)==2
    assert json.loads(runtime.calls[1]['messages'][0]['content'])['target_check_codes']==['CF-001']


def test_invalid_finding_type_has_exact_target_and_corrected_type_is_accepted():
    request=cf_request(); good=wire_fixture(_valid_payload(),catalog_for(request)); bad=copy.deepcopy(good)
    bad['check_results'][0]['findings'][0]['risk_type']='UNASSIGNED_RISK_TYPE'
    result,runtime=run_cf(request,bad,{'check_results':[good['check_results'][0]]})
    repair=json.loads(runtime.calls[1]['messages'][0]['content'])
    assert result.status=='COMPLETED' and repair['target_check_codes']==['CF-001']
    assert repair['errors'][0]['path']==['check_results',0,'findings',0,'risk_type']


@pytest.mark.parametrize('field,value', [('status', {}), ('status', []), ('findings', [None]),
    ('findings', [7]), ('findings', [{'primary_evidence_source_ids': {}}])])
def test_malformed_model_fields_never_escape_as_python_runtime_exceptions(field,value):
    request=cf_request(); good=wire_fixture(_valid_payload(),catalog_for(request)); bad=copy.deepcopy(good)
    bad['check_results'][0][field]=value
    result,runtime=run_cf(request,bad,{'check_results':[good['check_results'][0]]})
    assert result.status=='COMPLETED' and len(runtime.calls)==2


def test_shared_anchor_fact_can_be_selected_by_payment_and_acceptance_checks():
    request=cf_request()
    original=request.projected_ir_items[0]
    extra=original.model_copy(deep=True,update={'item_id':'shared-anchor-other-fact','ir_type':'payment_terms'})
    request.projected_ir_items.append(extra)
    scope=next(s for s in request.check_task_scopes if s.check_code=='CF-008')
    scope.expected_item_ids.append(extra.item_id); scope.provided_item_ids.append(extra.item_id)
    prompt,ir,anchors,_=cf._prompt(request)
    catalog=DirectSourceCatalog.build(request,ir,anchors)
    payload=json.loads(prompt.split('\n',1)[1])
    for field in ('payment_source_ids','trigger_material_source_ids'):
        assert set(payload['cf005_candidate'][field]).issubset(catalog.allowed['CF-005'])
    extra_ids={sid for sid,b in catalog.bindings.items() if ir[b['ir_ref']].item_id==extra.item_id}
    assert extra_ids and extra_ids.issubset(catalog.allowed['CF-008'])
    assert extra_ids <= catalog.allowed['CF-005']


@pytest.mark.parametrize('factory',[_po_request,_icd_request])
def test_repair_preserves_full_law_and_allows_target_explanation_to_be_corrected(factory):
    request=factory()
    request.legal_evidence=_bundle('必须保留的法条正文与例外。'*130,domain=request.unit_id,
        check_codes=[s.check_code for s in request.assigned_check_specs]).evidence
    request.legal_evidence_prompt_status='INCLUDED'
    good=_po_candidate_payload(request); bad=copy.deepcopy(good)
    target=bad['candidate_decisions'][0]
    target['primary_evidence_source_ids']=['wrong-source']; target['decision_summary']='第一次的错误说明'
    runtime=Runtime(bad,good)
    result=asyncio.run(base.GenericBaseDirectReviewer(runtime_factory=lambda _:runtime).review(request,tenant_id='offline',model_id='fake'))
    repair=json.loads(runtime.calls[1]['messages'][0]['content'])
    assert result.status=='COMPLETED'
    assert target['candidate_id'] in repair['target_candidate_ids']
    assert request.legal_evidence[0].unit.content in json.dumps(repair,ensure_ascii=False)
    assert not any('不得改变' in s or '只修改primary' in s for s in repair['constraints'])


def test_candidate_repair_can_correct_verdict_and_evidence_together_but_not_siblings():
    before={key:dict(candidate_id=key,verdict='RISK',decision_summary='旧判断',primary_evidence_source_ids=['old']) for key in ('target','sibling')}
    after=copy.deepcopy(before); after['target'].update(verdict='NO_RISK',decision_summary='原文推翻旧判断',primary_evidence_source_ids=['new'])
    base._validate_po_decision_semantic_preservation(before,after,repair_type='EVIDENCE_SELECTION_REPAIR',repair_target_candidate_ids=('target',))
    after['sibling']['decision_summary']='无关改写'
    with pytest.raises(cf.DirectReviewError,match='sibling'):
        base._validate_po_decision_semantic_preservation(before,after,repair_type='EVIDENCE_SELECTION_REPAIR',repair_target_candidate_ids=('target',))


def test_evidence_repair_cannot_change_a_non_target_source_selection():
    before={key:dict(candidate_id=key,verdict='RISK',primary_evidence_source_ids=['old']) for key in ('target','sibling')}
    after=copy.deepcopy(before); after['sibling']['primary_evidence_source_ids']=['new']
    with pytest.raises(cf.DirectReviewError,match='sibling'):
        base._validate_po_decision_semantic_preservation(before,after,repair_type='EVIDENCE_SELECTION_REPAIR',repair_target_candidate_ids=('target',))


@pytest.mark.parametrize('value',[None,{},[],1,[{}]])
def test_candidate_repair_helpers_do_not_crash_on_malformed_counter(value):
    request=_po_request(); good=_po_candidate_payload(request); bad=copy.deepcopy(good)
    bad['candidate_decisions'][0]['counter_evidence_source_ids']=value
    runtime=Runtime(bad,good)
    result=asyncio.run(base.GenericBaseDirectReviewer(runtime_factory=lambda _:runtime).review(request,tenant_id='offline',model_id='fake'))
    assert result.status=='COMPLETED' and len(runtime.calls)==2


@pytest.mark.parametrize('value',[None,{},[],1])
def test_unlocatable_candidate_id_is_a_structured_error_not_a_repair_runtime_crash(value):
    request=_po_request(); bad=_po_candidate_payload(request)
    bad['candidate_decisions'][0]['candidate_id']=value
    runtime=Runtime(bad)
    with pytest.raises(cf.DirectReviewError) as error:
        asyncio.run(base.GenericBaseDirectReviewer(runtime_factory=lambda _:runtime).review(request,tenant_id='offline',model_id='fake'))
    assert error.value.code=='RISK_CANDIDATE_MISSING' and len(runtime.calls)==1


@pytest.mark.parametrize('text,before,after',[
    ('合同签订后甲方预付80%价款，剩余20%验收合格后支付。',True,False),
    ('合同签订后甲方预付80%价款，逾期付款应承担违约金。',True,False),
    ('验收合格后甲方一次性付清全部价款。',False,True),
    ('甲方收到发票后支付全部价款。',False,False),
])
def test_payment_events_do_not_borrow_other_events_timing_or_penalty(text,before,after):
    candidate=cf._prompt(full_payment_request(text))[3]
    assert candidate.payment_ir_refs
    assert candidate.payment_before_performance==before
    assert candidate.trigger_absence_verified==after


def test_payer_is_bound_to_payment_verb_not_delivery_ir_subject():
    request=full_payment_request('乙方交货后，甲方应一次性支付全部价款。')
    request.projected_ir_items[0]=request.projected_ir_items[0].model_copy(update={
        'ir_type':'delivery_terms','subject':'乙方','predicate':'交货','object':'设备'})
    assert cf._prompt(request)[3].payer_role_status=='OUR_PARTY'


@pytest.mark.parametrize('unclear', ['第三次支付：于乙方完成货品交付。',
    '若因车辆限制，我方将按市场价格向买方支付相应的运输费用。'])
def test_advance_payer_is_not_made_unknown_by_unrelated_payment_mentions(unclear):
    request=full_payment_request('自合同签订且乙方出具发票之日起10日内，甲方向乙方支付合同总价之25%。'+unclear)
    assert cf._prompt(request)[3].payer_role_status=='OUR_PARTY'
    request=request.model_copy(update={'perspective':'PARTY_B','our_party':request.counterparty,'counterparty':request.our_party})
    assert cf._prompt(request)[3].payer_role_status=='COUNTERPARTY'


def test_unresolved_advance_payer_is_not_borrowed_from_later_payment():
    request=full_payment_request('合同签订后预付25%价款。验收合格后甲方支付剩余75%价款。')
    assert cf._prompt(request)[3].payer_role_status=='AMBIGUOUS'


@pytest.mark.parametrize('text',[
    '合同签订后甲方一次性支付全部价款，乙方无需提供履约保函，也不提供担保。',
    '甲方预付全部价款，履约保函：____。',
])
def test_negated_or_blank_safeguard_is_not_present(text):
    request=full_payment_request(text); cat=catalog_for(request)
    assert 'PERFORMANCE_GUARANTEE' not in cf._prompt(request)[3].identified_security_mechanisms
    bad=wire_fixture(_valid_payload(),cat)
    check=bad['check_results'][4]
    check.update(candidate_decision='RISK_NOT_CONFIRMED',findings=[],identified_security_mechanisms=['PERFORMANCE_GUARANTEE'],
        candidate_evidence_source_ids=[next(iter(cat.allowed['CF-005']))],decision_note='有保函，因此无风险。')
    with pytest.raises(cf.DirectReviewError):
        run_cf(request,bad,bad)


def test_unknown_scope_cannot_get_server_generated_absence():
    request=full_payment_request('合同签订后甲方一次性支付全部价款。'); request.check_task_scopes=[]
    cat=catalog_for(request); raw=wire_fixture(_valid_payload(),cat)
    finding=copy.deepcopy(raw['check_results'][0]['findings'][0]); finding.update(check_code='CF-005',risk_type='ADVANCE_PAYMENT_SECURITY_RISK')
    check=raw['check_results'][4]
    check.update(candidate_decision='RISK_CONFIRMED',findings=[finding],candidate_evidence_source_ids=[],identified_security_mechanisms=[])
    with pytest.raises(cf.DirectReviewError,match='ABSENCE'):
        run_cf(request,raw,raw)


@pytest.mark.parametrize('unit',hz.HORIZONTAL_UNIT_IDS)
def test_failed_horizontal_completion_still_records_usage(unit):
    candidate=horizontal_candidate(unit)
    batch=hz.HorizontalBatch(batch_id='risk-batch-'+'4'*32,unit_id=unit,check_codes=[candidate.check_code],
        candidate_ids=[candidate.candidate_id],evidence_source_ids=['left','right'],estimated_business_context_tokens=10)
    plan=NS(batches=[batch],candidates=[candidate],check_codes=[candidate.check_code])
    runtime=Runtime({'candidate_decisions':[]},{'candidate_decisions':[]})
    with patch.object(hz,'_batch_prompt_details',return_value=('{}',[],0,'NOT_REQUESTED')),patch.object(hz,'_materialize_unit',side_effect=lambda **kw:kw):
        result=asyncio.run(hz.execute_horizontal_unit(NS(review_id='offline',attempt_no=1),plan,unit,
            tenant_id='offline',model_id='fake',runtime_factory=lambda _:runtime))
    metric=result['metrics'][0]
    assert metric.status=='FAILED' and metric.model_call_count==len(runtime.calls)==2
    assert metric.repair_count==1
    assert metric.total_tokens is not None and metric.total_tokens>0


def test_missing_attachment_risk_requires_trigger_and_absence():
    candidate=horizontal_candidate('missing_ambiguity_completeness')
    raw=hz.HorizontalDecisionRaw(candidate_id=candidate.candidate_id,verdict='RISK',decision_summary='附件未提供',
        primary_evidence_source_ids=['absent'],recommended_control_codes=['CLARIFY'])
    with pytest.raises(hz.HorizontalReviewError,match='trigger'):
        hz._validate_decision(raw,candidate)
    assert hz._validate_decision(raw.model_copy(update={'primary_evidence_source_ids':['left','absent']}),candidate)


def test_ambiguous_names_never_bind_both_versions_without_explicit_ids():
    from contract.legal_evidence.prompting import selected_legal_evidence_ids,legal_citation_label,review_legal_evidence_catalog,merge_legal_reasoning
    one=_bundle('第一版条文',domain='commercial_financial',check_codes=['CF-001']).evidence[0]
    two=one.model_copy(deep=True); two.evidence_id='legal-evidence-'+'b'*32
    two.unit=two.unit.model_copy(update={'version_id':'another-version','content':'另一版条文'})
    issue=legal_citation_label(one)+'规定了相关义务。'
    assert selected_legal_evidence_ids([one,two],[],prompt_included=True)==[]
    assert all(row['citation_ambiguous_for_checks']==['CF-001'] for row in review_legal_evidence_catalog([one,two])[0])
    assert merge_legal_reasoning(issue,[],[one,two],'CF-001',prompt_included=True,selected_ids=[]) == issue
    assert selected_legal_evidence_ids([one,two],[one.evidence_id],prompt_included=True)==[one.evidence_id]


def test_negative_direct_judgment_requires_selected_basis_and_persists_anchor():
    request=fva_request(); cat=catalog_for(request)
    good=wire_fixture(fva_payload(),cat)
    for check in good['check_results']:
        check['findings']=[]
        check['decision_evidence_source_ids']=sorted(cat.allowed[check['check_code']])
    bad=copy.deepcopy(good)
    bad['check_results'][0]['decision_evidence_source_ids']=[]
    with pytest.raises(SourceSelectionError,match='decision_evidence_source_ids'):
        cat.decode(bad)
    runtime=Runtime(good)
    result=asyncio.run(base.GenericBaseDirectReviewer(runtime_factory=lambda _:runtime).review(request,tenant_id='offline',model_id='fake'))
    assert result.status=='COMPLETED'
    assert result.check_results[0].decision_anchor_ids


def test_each_attachment_is_matched_by_identity_and_scope_count_is_real():
    from contract.risk.models import Perspective
    generation='generation-audit'; nodes=[]; sources=[]; blocks=[]
    for number,text in enumerate(('范围详见附件一。','价格详见附件二。'),1):
        block='block-'+str(number)
        sid=hz._stable_id('horizontal-es',{'generation_id':generation,'block_id':block,'tag':'attachment_reference'})
        nodes.append(hz.RelationshipNode(node_id='horizontal-node-'+str(number)*32,ir_type='attachment_reference',block_id=block,
            block_no=number,predicate='引用',normalized_topic='附件',normalized_value=text,source_text=text,char_start=0,char_end=len(text)))
        sources.append(hz.HorizontalEvidenceSource(source_id=sid,generation_id=generation,block_id=block,block_no=number,
            char_start=0,char_end=len(text),quoted_text=text,quoted_text_hash='sha256:'+hashlib.sha256(text.encode()).hexdigest()))
        blocks.append(NS(block_id=block,heading_path=[]))
    index=hz.ContractRelationshipIndex(generation_id=generation,attachment_references=nodes,all_nodes=nodes,
        raw_pair_count=1,indexed_pair_count=1,discarded_pair_count=0,index_hash='sha256:'+'a'*64)
    value=NS(perspective=Perspective.PARTY_A,our_party='甲方',counterparty='乙方',review_id='audit',generation_id=generation,source_blocks=blocks)
    with patch.object(hz,'build_relationship_index',return_value=(index,sources)):
        missing=hz.build_horizontal_plan(value,NS(units=[]),assigned_check_codes=['MAC-005'])
        assert len(missing.candidates)==2
        assert all('全部2个Block' in a.checked_scope and '93' not in a.checked_scope for a in missing.absence_evidence_sources)
        value.source_blocks=[*blocks,NS(block_id='attachment-one',heading_path=['附件1'])]
        partial=hz.build_horizontal_plan(value,NS(units=[]),assigned_check_codes=['MAC-005'])
        assert len(partial.candidates)==1 and '附件:2' in partial.absence_evidence_sources[0].missing_target
