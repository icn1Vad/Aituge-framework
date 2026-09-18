import copy
from decimal import Decimal
import pytest
from business_workflow_kit import SceneRegistry, load_builtin_scenes
from business_workflow_kit.audit import AuditContext, AuditRecord, AuditRunner, EvidenceCatalog
from business_workflow_kit.installments import check_installments, money


def test_scenes_have_disjoint_workflows_and_unknown_modes_are_empty():
    registry = load_builtin_scenes()
    travel = {w['workflowType'] for w in registry.workflows('TRAVEL_ASSISTANT')}
    huatai = {w['workflowType'] for w in registry.workflows('HUATAI_ASSISTANT')}
    assert len(travel) == len(huatai) == 2
    assert not travel & huatai
    assert registry.workflows('UNKNOWN_ASSISTANT') == []


def test_registry_cannot_be_changed_through_a_returned_value():
    registry = load_builtin_scenes()
    registry.scenes()[0]['workflows'].clear()
    assert all(scene['workflows'] for scene in registry.scenes())


@pytest.mark.parametrize('mutate', [
    lambda s: s.append(copy.deepcopy(s[0])),
    lambda s: s[0].update(protocolVersion=999),
    lambda s: s[0]['workflows'].append(copy.deepcopy(s[0]['workflows'][0])),
    lambda s: s[0]['workflows'][0]['fields'].append({'key':'x','type':'enum'}),
])
def test_invalid_manifests_fail_at_registration(mutate):
    scenes = load_builtin_scenes().scenes()
    mutate(scenes)
    with pytest.raises(ValueError): SceneRegistry(scenes)


def test_evidence_is_bound_by_backend_and_can_cross_check_categories():
    catalog = EvidenceCatalog('contract-1', 'v1', [
        {'source_id':'A028','block_id':'b1','page_number':5,'text':'验收合格后付款'}])
    bound = catalog.select(['A028'], document_version='v1', exposed_ids=frozenset({'A028'}))[0]
    assert bound.quoted_text == '验收合格后付款'
    assert bound.char_end == len(bound.quoted_text)
    assert len(bound.quoted_text_hash) == 64


@pytest.mark.parametrize('refs,version,exposed', [
    (['A999'],'v1',{'A999'}), (['A028'],'v2',{'A028'}),
    (['A028'],'v1',set()), (['A028','A028'],'v1',{'A028'}),
])
def test_unbound_references_rejected(refs, version, exposed):
    catalog = EvidenceCatalog('c','v1',[{'source_id':'A028','block_id':'b','text':'原文'}])
    with pytest.raises(ValueError): catalog.select(refs, document_version=version, exposed_ids=frozenset(exposed))


def test_one_failed_check_does_not_discard_siblings_or_become_a_pass():
    runner = AuditRunner()
    runner.register('OK', lambda ctx: [AuditRecord('OK','PASS','通过')])
    runner.register('BROKEN', lambda ctx: 1/0)
    runner.register('EMPTY', lambda ctx: [])
    result = runner.run(AuditContext())
    assert [r.status for r in result.records] == ['PASS','ERROR','PENDING']
    assert result.status == 'PARTIAL' and not result.passed
    assert not AuditRunner().run(AuditContext()).passed


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '1.001', None, True, 0.1])
def test_money_rejects_ambiguous_or_invalid_values(value):
    with pytest.raises(ValueError): money(value)


def sample():
    return {'installments': {
        'second': {'amount':'290751.90','used_amount':'0','currency':'CNY','conditions_met':True,'evidence_refs':['A005']},
        'third': {'amount':'96917.30','used_amount':'0','currency':'CNY','conditions_met':None,'evidence_refs':['A006']},
    }, 'expense_lines': [
        {'id':'line-1','installment_id':'second','amount':'290751.90','currency':'CNY','association_confirmed':True},
        {'id':'line-2','installment_id':'third','amount':'96917.30','currency':'CNY','association_confirmed':True},
    ]}


def test_distinct_installments_are_not_compared_as_one_total():
    rows = check_installments(AuditContext(sample()))
    assert [r.status for r in rows] == ['PASS','PENDING']


def test_multiple_lines_cannot_each_spend_the_full_period_limit():
    facts = sample()
    facts['expense_lines'].append(dict(facts['expense_lines'][0], id='line-3', amount='0.01'))
    rows = check_installments(AuditContext(facts))
    assert rows[0].status == rows[2].status == 'RISK'


@pytest.mark.parametrize('change', [
    {'used_amount':None}, {'conditions_met':None}, {'currency':None}, {'evidence_refs':[]},
])
def test_missing_context_is_pending_not_zero_or_pass(change):
    facts=sample(); facts['installments']['second'].update(change)
    assert check_installments(AuditContext(facts))[0].status == 'PENDING'


def test_unknown_association_is_not_inferred_from_equal_amount():
    facts=sample(); facts['expense_lines'][0]['association_confirmed']=False
    assert check_installments(AuditContext(facts))[0].status == 'PENDING'


def test_history_counts_against_the_current_request():
    facts=sample(); facts['installments']['second']['used_amount']='0.01'
    assert check_installments(AuditContext(facts))[0].status == 'RISK'


def test_failed_conditions_do_not_pass_when_amount_is_within_limit():
    facts=sample(); facts['installments']['third']['conditions_met']=False
    assert check_installments(AuditContext(facts))[1].status == 'RISK'


def test_invalid_sibling_prevents_false_same_period_pass():
    facts=sample(); facts['expense_lines'].append(dict(facts['expense_lines'][0], id='bad', amount=None))
    assert check_installments(AuditContext(facts))[0].status == 'PENDING'


@pytest.mark.parametrize('change', [{'currency':'USD'}, {'association_confirmed':False}])
def test_unconfirmed_sibling_cannot_create_a_false_same_period_risk(change):
    facts=sample()
    facts['expense_lines'].append(dict(facts['expense_lines'][0], id='unconfirmed', **change))
    rows=check_installments(AuditContext(facts))
    assert rows[0].status == rows[2].status == 'PENDING'
