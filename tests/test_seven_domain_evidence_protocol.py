"""Offline wire-to-Finding tests: no provider credentials or network required."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from services.contract.capabilities.review_evidence_protocol import (
    DirectSourceCatalog, SourceSelectionError, direct_wire_schema, EVIDENCE_PROTOCOL_VERSION,
)
from services.contract.capabilities import risk_review as cf
from services.contract.capabilities import risk_review_bundle as base
from services.contract.capabilities import horizontal_review as hz
from contract.risk.review_ledger import CheckTaskScope
from test_contract_risk_direct_review import _request as cf_request, _valid_payload, _completion, _request_with_single_payment_text
from test_contract_risk_base_bundle import _request as fva_request, _payload as fva_payload, _po_request, _icd_request, _po_candidate_payload


def catalog_for(request):
    result = cf._prompt(request) if request.unit_id == cf.COMMERCIAL_UNIT_ID else base._generic_prompt(request)
    return DirectSourceCatalog.build(request, result[1], result[2])


def wire_fixture(raw, catalog, *, tolerate_invalid=False):
    """Explicit migration of trusted test fixtures, NOT a model-output fallback."""
    raw = copy.deepcopy(raw)
    def ids(evidence):
        values = []
        for e in evidence:
            if not isinstance(e, dict):
                values.append('invalid-legacy-fixture')
                continue
            matching = [sid for sid, b in catalog.bindings.items()
                if b['ir_ref'] == e.get('ir_ref') and b['evidence_ref'] == e.get('evidence_ref')
                and b['evidence_type'] == e.get('evidence_type')]
            if not matching and not tolerate_invalid:
                raise AssertionError('Fixture has no exact source binding')
            values.append(matching[0] if matching else 'invalid-legacy-fixture')
        return values
    checks = raw.get('check_results', raw.get('checks')) if isinstance(raw, dict) else None
    if not isinstance(checks, list):
        return raw
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('findings', []), list):
            continue
        # Trusted fake judgments explicitly select sources. This migration is
        # test-only; production never supplies a missing model decision basis.
        check.setdefault('decision_evidence_source_ids', sorted(catalog.allowed.get(check.get('check_code'), set()))
                         if not check.get('findings') and check.get('status') in {'REVIEWED','NOT_APPLICABLE'} else [])
        for finding in check.get('findings', []):
            if not isinstance(finding, dict) or 'evidence' not in finding:
                continue
            old = finding.pop('evidence')
            if not isinstance(old, list):
                finding['primary_evidence_source_ids'] = None
                continue
            finding['primary_evidence_source_ids'] = ids([e for e in old if not isinstance(e, dict) or e.get('evidence_type') != 'ABSENCE'])
            finding['absence_assessments'] = [{k: e[k] for k in ('checked_scope', 'verification_note')}
                for e in old if isinstance(e, dict) and e.get('evidence_type') == 'ABSENCE']
        if 'candidate_evidence' in check:
            old = check.pop('candidate_evidence')
            check['candidate_evidence_source_ids'] = ids(old) if isinstance(old, list) else None
    return raw


def normalized_fixture(raw, request=None):
    """Expected internal form including v3 model selections/server anchors."""
    catalog = catalog_for(request or cf_request())
    return catalog.decode(wire_fixture(raw, catalog))


class Runtime:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def complete_with_usage(self, **kwargs):
        self.calls.append(kwargs)
        assert self.outputs, 'Unexpected additional paid-model attempt'
        completion = _completion(json.dumps(self.outputs.pop(0), ensure_ascii=False), repair_no=len(self.calls)-1)
        from dataclasses import replace
        return replace(completion, review_unit_id=kwargs['review_unit_id'])


@pytest.mark.parametrize('factory,payload_factory,reviewer', [
    (cf_request, _valid_payload, cf.CommercialFinancialDirectReviewer),
    (fva_request, fva_payload, base.GenericBaseDirectReviewer),
])
def test_direct_real_runtime_selects_source_and_program_binds_original(factory, payload_factory, reviewer):
    request = factory()
    catalog = catalog_for(request)
    wire = wire_fixture(payload_factory(), catalog)
    runtime = Runtime(wire)
    result = asyncio.run(reviewer(runtime_factory=lambda _: runtime).review(request, tenant_id='test', model_id='fake'))
    assert result.status == 'COMPLETED' and result.model_call_count == 1
    assert result.findings
    assert all(e.anchor_id and e.quoted_text_hash for f in result.findings for e in f.evidence_candidates)
    sent = json.loads(runtime.calls[0]['messages'][0]['content'].split('\n', 1)[1])
    assert sent['source_selection_contract']['version'] == EVIDENCE_PROTOCOL_VERSION
    assert 'projected_ir' not in sent and 'source_excerpts' not in sent
    assert 'evidence' not in sent['output_contract']['finding_required_fields']
    if request.unit_id == cf.COMMERCIAL_UNIT_ID:
        assert set(sent['output_contract']['cf005_required_fields']) == {
            'candidate_decision', 'identified_security_mechanisms', 'candidate_evidence_source_ids'}


@pytest.mark.parametrize('factory,payload_factory', [(cf_request, _valid_payload), (fva_request, fva_payload)])
@pytest.mark.parametrize('bad', [None, '', [], ['legal-evidence-123'], ['I001'], ['A001'], ['missing']])
def test_both_direct_domains_reject_missing_legal_and_legacy_ids(factory, payload_factory, bad):
    catalog = catalog_for(factory())
    payload = wire_fixture(payload_factory(), catalog)
    finding = next(f for c in payload['check_results'] for f in c['findings'])
    finding['primary_evidence_source_ids'] = bad
    with pytest.raises(SourceSelectionError):
        catalog.decode(payload)


def test_old_type_and_pair_protocol_not_accepted_by_live_decoder():
    with pytest.raises(SourceSelectionError, match='Legacy'):
        catalog_for(cf_request()).decode(_valid_payload())


def test_one_selected_source_cannot_change_pair_or_copy_another_anchor():
    request = cf_request()
    catalog = catalog_for(request)
    payload = wire_fixture(_valid_payload(), catalog)
    first = payload['check_results'][0]['findings'][0]
    chosen = next(s for s, b in catalog.bindings.items() if b['ir_ref'] == 'I002')
    first['primary_evidence_source_ids'] = [chosen]
    bound = catalog.decode(payload)['check_results'][0]['findings'][0]['evidence']
    assert [(e['ir_ref'], e['evidence_ref']) for e in bound] == [('I002', 'A002')]
    first['primary_evidence_source_ids'] = [chosen, chosen]
    with pytest.raises(SourceSelectionError, match='Duplicate'):
        catalog.decode(payload)


def test_partial_absence_cannot_be_injected_as_contract_quote():
    request = cf_request()
    request.check_task_scopes = []
    payload = wire_fixture(_valid_payload(), catalog_for(request))
    finding = payload['check_results'][0]['findings'][0]
    finding['primary_evidence_source_ids'] = []
    finding['absence_assessments'] = [{'checked_scope': '全文', 'verification_note': '未见金额'}]
    with pytest.raises(SourceSelectionError, match='scope'):
        catalog_for(request).decode(payload)


def test_wire_repair_schema_has_no_legacy_type_or_pair_fields():
    for schema in (cf.ModelCheckCoverageResultRaw.model_json_schema(), base.GenericModelResponseRaw.model_json_schema()):
        wire = json.dumps(direct_wire_schema(schema))
        assert 'primary_evidence_source_ids' in wire
        for key in ('"evidence_type"', '"ir_ref"', '"evidence_ref"', '"candidate_evidence"'):
            assert key not in wire


def full_payment_request(text):
    request = _request_with_single_payment_text(text)
    # Real projections carry explicit scope completeness; a missing scope is unknown.
    request.check_task_scopes = [CheckTaskScope(check_code='CF-005',
        expected_item_ids=[i.item_id for i in request.projected_ir_items],
        provided_item_ids=[i.item_id for i in request.projected_ir_items],
        expected_anchor_ids=[a.anchor_id for a in request.source_excerpts],
        provided_anchor_ids=[a.anchor_id for a in request.source_excerpts])]
    return request


def test_payment_method_ir_does_not_erase_anchored_acceptance_condition():
    request = full_payment_request('项目建设完成且安装调试验收合格后，甲方收到发票后一次性付清。')
    request.projected_ir_items[0].predicate = '支付方式'
    request.projected_ir_items[0].object = '一次性付清'
    candidate = cf._prompt(request)[3]
    assert candidate.acceptance_linked and not candidate.payment_before_performance
    assert candidate.trigger_absence_verified


def test_invoice_is_not_itself_evidence_of_preperformance_payment():
    request = full_payment_request('甲方收到发票后支付合同总价的100%。')
    candidate = cf._prompt(request)[3]
    assert not candidate.payment_before_performance
    assert not candidate.trigger_absence_verified  # timing relative to acceptance is unknown


def test_real_advance_payment_still_identified():
    candidate = cf._prompt(full_payment_request('合同签订后甲方一次性支付全部价款。'))[3]
    assert candidate.payment_before_performance and candidate.substantial_prepayment


def test_wrong_type_and_wrong_payment_judgment_can_be_corrected_together():
    request = full_payment_request('验收合格后甲方一次性支付全部合同价款。')
    catalog = catalog_for(request)
    initial = wire_fixture(_valid_payload(), catalog)
    wrong = initial['check_results'][4]
    finding = copy.deepcopy(initial['check_results'][0]['findings'][0])
    finding.update(check_code='CF-005', risk_type='ADVANCE_PAYMENT_SECURITY_RISK', title='履约前付款风险')
    finding['primary_evidence_source_ids'] = ['legal-evidence-wrong']
    wrong.update(candidate_decision='RISK_CONFIRMED', findings=[finding], candidate_evidence_source_ids=[], identified_security_mechanisms=[])
    corrected = copy.deepcopy(wrong)
    corrected.update(candidate_decision='TRIGGER_NOT_MET', findings=[], decision_note='原文明确约定验收合格后付款，不是预付款。')
    runtime = Runtime(initial, {'check_results': [corrected]})
    result = asyncio.run(cf.CommercialFinancialDirectReviewer(runtime_factory=lambda _: runtime).review(request, tenant_id='test', model_id='fake'))
    assert result.status == 'COMPLETED' and result.model_call_count == 2
    assert not any(f.check_code == 'CF-005' for f in result.findings)
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert repair['repair_mode'] == 'REASSESS_TARGET' and repair['target_check_codes'] == ['CF-005']
    assert repair['context']['contract_evidence_catalog']


def test_repair_keeps_full_law_separate_and_persists_selected_citation():
    from test_legal_evidence_binding import _bundle
    request = cf_request()
    request.legal_evidence = _bundle('法条正文；' * 2200 + '条末例外不可省略。',
        domain='commercial_financial', check_codes=['CF-001']).evidence
    catalog = catalog_for(request)
    good = wire_fixture(_valid_payload(), catalog)
    from contract.legal_evidence.prompting import legal_citation_label
    good['check_results'][0]['findings'][0]['issue'] = legal_citation_label(request.legal_evidence[0]) + '：应结合本合同约定核实价款范围，适用性待核验。'
    good['check_results'][0]['findings'][0]['legal_evidence_ids'] = [request.legal_evidence[0].evidence_id]
    bad = copy.deepcopy(good)
    bad['check_results'][0]['findings'][0]['primary_evidence_source_ids'] = [request.legal_evidence[0].evidence_id]
    runtime = Runtime(bad, {'check_results': [good['check_results'][0]]})
    result = asyncio.run(cf.CommercialFinancialDirectReviewer(runtime_factory=lambda _: runtime).review(request, tenant_id='test', model_id='fake'))
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    law = repair['context']['legal_evidence_catalog'][0]
    assert law['content_excerpt'] == request.legal_evidence[0].unit.content
    assert law['evidence_id'] == request.legal_evidence[0].evidence_id
    assert result.findings[0].legal_evidence_ids == [request.legal_evidence[0].evidence_id]
    assert all(e.evidence_type == 'TEXT_QUOTE' for e in result.findings[0].evidence_candidates)


@pytest.mark.parametrize('unit', hz.HORIZONTAL_UNIT_IDS)
def test_horizontal_materialization_uses_selection_not_all_trigger_context(unit):
    import hashlib
    import time
    from contract.risk.models import Perspective
    candidate = horizontal_candidate(unit)
    candidate.candidate_type = 'DATE_CHRONOLOGY_CONFLICT' if unit.startswith('cross') else 'MISSING_REFERENCED_ATTACHMENT'
    ids = ['left', 'right'] if unit.startswith('cross') else ['left', 'absent']
    decision = hz._validate_decision(hz.HorizontalDecisionRaw(candidate_id=candidate.candidate_id,
        verdict='RISK', decision_summary='仅按所选原文判断。', primary_evidence_source_ids=ids,
        recommended_control_codes=['CLARIFY']), candidate)
    sources = [SimpleNamespace(source_id=sid, ir_id=sid+'-ir', anchor_id=sid+'-anchor', block_id=sid+'-block',
        block_no=index, char_start=0, char_end=len(text), quoted_text=text,
        quoted_text_hash='sha256:'+hashlib.sha256(text.encode()).hexdigest())
        for index, (sid, text) in enumerate([('left', '起始日2024年1月1日'), ('right', '签订日2024年2月1日'), ('context', '无关背景')], 1)]
    plan = SimpleNamespace(candidates=[candidate], evidence_sources=sources,
        absence_evidence_sources=[SimpleNamespace(source_id='absent', checked_scope='所提供合同正文及附件目录', verification_method='冻结附件引用扫描')],
        check_codes=[candidate.check_code])
    value = SimpleNamespace(perspective=Perspective.PARTY_A, our_party='甲方', counterparty='乙方')
    result = hz._materialize_unit(value=value, plan=plan, unit_id=unit, decisions=[decision], metrics=[], started=time.perf_counter())
    assert result.status == 'COMPLETED' and len(result.findings) == 1
    assert result.canonical_roots[0].primary_evidence_source_ids == ids
    assert not any(e.quoted_text == '无关背景' for e in result.findings[0].evidence_candidates)


def horizontal_candidate(unit='cross_clause_consistency'):
    return hz.HorizontalCandidate(candidate_id='horizontal-candidate-'+'1'*32, unit_id=unit,
        check_code='CCC-001' if unit.startswith('cross') else 'MAC-001',
        candidate_type='TERM_CONFLICT', candidate_strength='SEMANTIC_REVIEW', normalized_topic='付款期限',
        left_evidence_source_ids=['left'], right_evidence_source_ids=['right'], context_evidence_source_ids=['context'],
        absence_evidence_source_ids=[] if unit.startswith('cross') else ['absent'],
        required_trigger_conditions=['原文证明'], primary_evidence_requirements=['实际依据'],
        absence_evidence_requirements=[] if unit.startswith('cross') else ['范围检查'],
        canonical_root_type='TERM_CONFLICT', severity_rule_id='TERM_CONFLICT_V1',
        allowed_control_codes=['CLARIFY'], owner_type='HORIZONTAL', requires_model_decision=True)


@pytest.mark.parametrize('unit', hz.HORIZONTAL_UNIT_IDS)
def test_horizontal_primary_is_explicit_selected_and_checked(unit):
    candidate = horizontal_candidate(unit)
    ids = ['left', 'right'] if unit.startswith('cross') else ['left', 'absent']
    raw = hz.HorizontalDecisionRaw(candidate_id=candidate.candidate_id, verdict='RISK', decision_summary='依据原文',
        primary_evidence_source_ids=ids, recommended_control_codes=['CLARIFY'])
    decision = hz._validate_decision(raw, candidate)
    assert decision.primary_evidence_source_ids == ids and 'context' not in decision.primary_evidence_source_ids
    for bad in ([], ['legal-evidence-id'], ['left']):
        with pytest.raises(hz.HorizontalReviewError):
            hz._validate_decision(raw.model_copy(update={'primary_evidence_source_ids': bad}), candidate)
    wire = raw.model_dump()
    wire.pop('primary_evidence_source_ids')
    with pytest.raises(hz.HorizontalReviewError):
        hz._parse_decisions(json.dumps({'candidate_decisions': [wire]}), [candidate])


@pytest.mark.parametrize('factory', [_po_request, _icd_request])
def test_existing_candidate_domains_share_protocol_without_reverting_primary(factory):
    request = factory()
    prompt = json.loads(base._generic_prompt(request)[0].split('\n', 1)[1])
    assert EVIDENCE_PROTOCOL_VERSION in json.dumps(prompt)
    runtime = Runtime(_po_candidate_payload(request))
    # Full domain-specific fake decision helpers remain covered by existing suites;
    # this checks their prompt shares the new source/law separation contract.
    assert 'primary_evidence_source_ids' in json.dumps(prompt)
