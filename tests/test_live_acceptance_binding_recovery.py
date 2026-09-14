"""Second live acceptance regressions; all providers are recorded/fake."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from services.contract.capabilities import horizontal_review as hz
from services.contract.capabilities.review_evidence_protocol import SourceSelectionError
from test_seven_domain_evidence_protocol import catalog_for, cf_request, _valid_payload, wire_fixture, horizontal_candidate


@pytest.mark.parametrize('placeholder', ['___', ' / ', '：', '……'])
def test_relationship_index_keeps_placeholder_nodes_without_inventing_values(placeholder):
    from risk_test_data import risk_plan_input
    from contract.risk.models import RiskReviewPlanInput
    raw = risk_plan_input().model_dump(mode='json')
    item = raw['stage_result']['semantic_ir']['payment_terms'][0]
    item.update(subject=placeholder, object=placeholder)
    value = RiskReviewPlanInput.model_validate(raw)
    index, sources = hz.build_relationship_index(value)
    node = next(n for n in index.all_nodes if n.ir_id == item['item_id'])
    assert node.normalized_value == '' and node.normalized_topic == 'paymentterms'
    assert node.object == placeholder and node.source_text == value.source_blocks[0].text
    assert any(s.ir_id == node.ir_id and s.quoted_text == node.source_text for s in sources)


def test_relationship_index_keeps_pure_placeholder_source_block():
    from risk_test_data import risk_plan_input
    from contract.risk.models import RiskSourceBlock
    value = risk_plan_input()
    value.source_blocks.append(RiskSourceBlock(block_id='empty-slot', block_no=2, text='______'))
    index, sources = hz.build_relationship_index(value)
    node = next(n for n in index.all_nodes if n.block_id == 'empty-slot')
    assert node.normalized_value == '' and node.ir_type == 'placeholder'
    assert any(s.quoted_text == '______' for s in sources)


def test_single_finding_reuses_only_explicit_model_check_selection():
    catalog = catalog_for(cf_request())
    payload = wire_fixture(_valid_payload(), catalog)
    check = next(c for c in payload['check_results'] if c['findings'])
    finding = check['findings'][0]
    check['findings'] = [finding]
    selected = list(finding['primary_evidence_source_ids'])
    finding['primary_evidence_source_ids'] = []
    check['decision_evidence_source_ids'] = selected
    original = copy.deepcopy(payload)
    result = catalog.decode(payload)
    resolved = next(c for c in result['check_results'] if c['check_code'] == check['check_code'])
    assert resolved['findings'][0]['evidence'] == [catalog.bindings[s] for s in selected]
    assert payload == original  # immutable raw replay input


@pytest.mark.parametrize('kind', ['missing', 'unknown', 'legal', 'multiple', 'malformed'])
def test_check_selection_cannot_invent_or_ambiguously_assign_evidence(kind):
    catalog = catalog_for(cf_request())
    payload = wire_fixture(_valid_payload(), catalog)
    check = next(c for c in payload['check_results'] if c['findings'])
    check['findings'] = [check['findings'][0]]
    finding = check['findings'][0]
    check['decision_evidence_source_ids'] = list(finding['primary_evidence_source_ids'])
    finding['primary_evidence_source_ids'] = []
    if kind == 'missing': check['decision_evidence_source_ids'] = []
    if kind == 'unknown': check['decision_evidence_source_ids'] = ['unknown']
    if kind == 'legal': check['decision_evidence_source_ids'] = ['legal-evidence-123']
    if kind == 'multiple': check['findings'].append(copy.deepcopy(finding))
    if kind == 'malformed': finding['primary_evidence_source_ids'] = 'not-an-array'
    with pytest.raises(SourceSelectionError):
        catalog.decode(payload)


@pytest.mark.parametrize('primary,supporting', [(['absent'], ['left']), (['left'], ['absent']), (['left','absent'], [])])
def test_absence_and_trigger_can_be_selected_in_either_positive_evidence_column(primary, supporting):
    from test_semantic_review_acceptance_regressions import row
    candidate = horizontal_candidate('missing_ambiguity_completeness')
    raw = row(candidate)
    raw.update(primary_evidence_source_ids=primary, supporting_evidence_source_ids=supporting)
    decision = hz._validate_decision(hz.HorizontalDecisionRaw.model_validate(raw), candidate)
    assert decision.verdict == 'RISK'
    assert decision.primary_evidence_source_ids == primary
    assert decision.supporting_evidence_source_ids == supporting


def test_counterevidence_cannot_be_used_as_the_missing_positive_trigger():
    from test_semantic_review_acceptance_regressions import row
    candidate = horizontal_candidate('missing_ambiguity_completeness')
    raw = row(candidate)
    raw.update(primary_evidence_source_ids=['absent'], counter_evidence_source_ids=['left'])
    with pytest.raises(hz.HorizontalReviewError):
        hz._validate_decision(hz.HorizontalDecisionRaw.model_validate(raw), candidate)


def test_revision_large_successful_completion_is_not_discarded(monkeypatch):
    from services.contract.capabilities import revision_drafts as rev
    from test_contract_revision_drafts import _source
    source = _source()
    item = rev._plan_draft(source, source.findings[0], source.evidences,
        rev.compute_revision_key(source.review_id, source.generation_id, source.result_hash, source.findings[0].finding_id))
    calls = []
    async def completion(*args, **kwargs):
        calls.append(kwargs)
        return NS(content=json.dumps({'drafts':[dict(revision_key=item.revision_key, replacement_text='新增需求由双方书面确认。')]}),
            prompt_tokens=18000, completion_tokens=100, cached_tokens=0)
    monkeypatch.setattr(rev, '_complete_revision_batch', completion)
    result = asyncio.run(rev.LlmRevisionTextGenerator('42','fake',runtime=object()).generate([item], source=source))
    assert len(calls) == 1 and result.prompt_tokens == 18000 and len(result.items) == 1


def test_revision_keeps_long_paragraphs_and_all_insertion_candidates():
    from services.contract.capabilities import revision_drafts as rev
    from test_contract_revision_drafts import _source
    source = _source()
    long_text = '双方应书面确认调整内容并明确履行条件。' * 120
    block = rev.RevisionDocumentBlock(block_id='long-block', block_no=1, block_type='paragraph',
        char_start=0, char_end=len(long_text), text=long_text, heading_path=['条款'])
    assert rev._candidate_from_block(block, match_score=1).anchor_excerpt == long_text
    item = rev.RevisionNumberingPlanItem(item_id='item', level=0, marker_type='DECIMAL', text=long_text)
    assert item.text == long_text
    assert rev._validate_replacement(long_text, '双方书面确认。', source) == long_text
    assert rev._validate_supplement(long_text, source) == long_text
    source = source.model_copy(update={'document_blocks': [block.model_copy(update={
        'block_id':f'b{i}', 'block_no':i+1, 'heading_path':[f'独立条款{i}']}) for i in range(30)]})
    candidates = rev._build_insertion_candidates(source, source.findings[0], [], [])
    assert len(candidates) == 30


def test_semantic_ids_bind_exact_names_and_preserve_multiple_real_roles(tmp_path):
    from test_rule_semantic_selection import resolve
    class Runtime:
        calls = 0
        async def complete_with_usage(self, **kwargs):
            self.calls += 1
            payload = json.loads(kwargs['messages'][0]['content'])
            ref = payload['fragments'][0]['ref']
            def selection(catalogue, names):
                return [dict(option_id=k, reason='合同明确约定我方承担该项实际义务', source_refs=[ref])
                    for k, name in catalogue.items() if name in names]
            answer = dict(status='RESOLVED', reason='我方出售设备并承担独立服务义务',
                role_selections=selection(payload['role_catalog'], ['出卖方','服务方']),
                type_selections=selection(payload['contract_type_catalog'], ['采购合同','技术服务合同']))
            return NS(content=json.dumps(answer), prompt_tokens=150, completion_tokens=80)
    runtime = Runtime()
    result = resolve(tmp_path, runtime)
    assert result['status'] == 'RESOLVED' and set(result['business_roles']) == {'出卖方','服务方'}
    assert result['source_fragments'] and len(result['contract_types']) == 2
    assert resolve(tmp_path, runtime)['cache_hit'] and runtime.calls == 1


def test_semantic_failure_keeps_precise_raw_diagnostic_private(tmp_path, monkeypatch):
    from test_rule_semantic_selection import Runtime, resolve
    directory = tmp_path / 'private'
    monkeypatch.setenv('CONTRACT_REVIEW_DIAGNOSTIC_DIR', str(directory))
    runtime = Runtime()
    runtime.answer['contract_types'] = ['自造合同类别']
    result = resolve(tmp_path / 'cache', runtime)
    assert result['status'] == 'UNRESOLVED' and '自造合同类别' not in json.dumps(result, ensure_ascii=False)
    record = json.loads(next(directory.glob('*.json')).read_text())
    assert record['payload']['raw_content'] and '自造合同类别' in str(record['payload']['errors'])
    assert resolve(tmp_path / 'cache', runtime)['cache_hit'] and len(runtime.calls) == 1


@pytest.mark.parametrize('corruption', ['source','legacy_quote','missing','duplicate','schema','foreign'])
def test_rule_failure_does_not_discard_a_valid_sibling(tmp_path, monkeypatch, corruption):
    from test_rule_library_reviewer import FakeModel
    from test_rule_library_shadow import snapshot
    from contract.risk.plan_builder import RiskReviewPlanBuilder
    from contract.rule_evidence.shadow import RuleLibraryShadow
    from contract.rule_evidence.reviewer import RuleLibraryReviewer
    from risk_test_data import risk_plan_input
    monkeypatch.setenv('CONTRACT_REVIEW_DIAGNOSTIC_DIR', str(tmp_path / 'private'))
    plan = RiskReviewPlanBuilder().build(risk_plan_input())
    observation = RuleLibraryShadow(snapshot(tmp_path)).evaluate(plan, tenant_id='42', contract_type_name='采购合同', business_role='买受方')
    first = observation['bundle']['evidence'][0]
    second = copy.deepcopy(first)
    second['evidence_id'] = 'rule-evidence-' + 'b' * 32
    observation['bundle']['evidence'].append(second)
    class Runtime(FakeModel):
        async def complete_with_usage(self, **kwargs):
            completion = await super().complete_with_usage(**kwargs)
            response = json.loads(completion.content)
            rows = response['decisions']
            assert len(rows) == 2
            if corruption == 'source': rows[1]['primary_evidence_source_ids'] = ['foreign-contract-source']
            if corruption == 'legacy_quote': rows[1]['quotes'] = [{'source_id': 'foreign', 'quote': '伪造合同条款'}]
            if corruption == 'missing': rows.pop()
            if corruption == 'duplicate': rows.append(copy.deepcopy(rows[1]))
            if corruption == 'schema': rows[1]['outcome'] = 'INVALID'
            if corruption == 'foreign': rows[1]['evidence_id'] = 'foreign-rule'
            completion.content = json.dumps(response, ensure_ascii=False)
            return completion
    runtime = Runtime()
    result = asyncio.run(RuleLibraryReviewer(runtime, max_prompt_chars=16000).review(observation, plan, tenant_id='42', model_id='fake'))
    assert runtime.calls == 1 and result.status == 'PARTIAL'
    assert [d.evidence_id for d in result.decisions] == [first['evidence_id']]
    assert result.pending_evidence_ids == [second['evidence_id']]
    record = json.loads(next((tmp_path / 'private').glob('*.json')).read_text())
    assert record['payload']['raw_content'] and record['payload']['errors']
