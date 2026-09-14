"""Offline coverage for the unified card/persistence result contract."""
import asyncio
import copy
import pytest

from contract.api.models import ReviewResultData, PublicReviewResultData
from contract.rule_evidence.execution import RuleLibraryExecution
from contract.rule_evidence.finding_projection import merge_rule_findings
from risk_test_data import risk_plan_input
from test_rule_library_shadow import snapshot
from test_rule_library_reviewer import FakeModel


def fixture(tmp_path):
    value = risk_plan_input()
    execution = RuleLibraryExecution(snapshot(tmp_path), tmp_path/'cache', runtime_factory=lambda _: FakeModel())
    rules = asyncio.run(execution.run(value, '42', 'fake', contract_type_name='采购合同', business_role='买受方'))
    assert rules.status == 'COMPLETED' and rules.decisions
    formal = dict(schema_version='1.0', review_id=value.review_id, business_task_id='10001',
        contract_version_id='20001', contract_profile=dict(contract_type='AUTO',
            party_a={'name':value.our_party}, party_b={'name':value.counterparty},
            perspective='PARTY_A', our_party=value.our_party, counterparty=value.counterparty, review_attitude='NEUTRAL'),
        summary=dict(overview='合同审查结果', high_count=0, medium_count=0, low_count=0, info_count=0),
        findings=[], evidences=[], legal_evidences=[], relationships=[])
    return value, rules, formal


def validate(formal):
    return ReviewResultData.model_validate({**formal, 'result_hash':'sha256:'+'1'*64})


def test_rule_risks_use_normal_findings_and_original_locations(tmp_path):
    value, rules, formal = fixture(tmp_path)
    merged, linked = merge_rule_findings(formal, rules, value)
    result = validate(merged)
    assert not formal['findings'] and all(d.finding_id is None for d in rules.decisions)
    assert len(result.findings) == len(linked.decisions)
    assert {d.finding_id for d in linked.decisions} == {f.finding_id for f in result.findings}
    assert result.summary.medium_count == len(result.findings)
    for evidence in result.evidences:
        block = next(b for b in value.source_blocks if b.block_id == evidence.block_id)
        assert block.text[evidence.char_start:evidence.char_end] == evidence.quoted_text
    public = PublicReviewResultData.from_internal(result)
    assert len(public.findings) == len(result.findings)
    assert public.summary.finding_count == len(result.findings)
    # Keep rule version/status for traceability, never invent legal citations.
    assert linked.evidence[0].version and linked.evidence[0].source_status == 'pending'
    assert all(not f.legal_evidence_ids for f in result.findings)
    twice, _ = merge_rule_findings(merged, linked, value)
    assert twice == merged


@pytest.mark.parametrize('outcome', ['NO_RISK', 'INSUFFICIENT_EVIDENCE'])
def test_non_risk_decisions_do_not_create_cards(tmp_path, outcome):
    value, rules, formal = fixture(tmp_path)
    for decision in rules.decisions:
        decision.outcome = outcome
    merged, linked = merge_rule_findings(formal, rules, value)
    assert not validate(merged).findings
    assert all(d.finding_id is None for d in linked.decisions)


@pytest.mark.parametrize('mutation', ['generation', 'perspective', 'location'])
def test_wrong_generation_party_or_original_location_cannot_be_projected(tmp_path, mutation):
    value, rules, formal = fixture(tmp_path)
    if mutation == 'generation':
        rules.generation_id = 'another-generation'
    elif mutation == 'perspective':
        rules.perspective = 'PARTY_B'
    else:
        rules.decisions[0].citations[0].char_start += 1
    with pytest.raises(ValueError):
        merge_rule_findings(formal, rules, value)


def test_duplicate_risk_merges_but_shared_quote_alone_does_not(tmp_path):
    value, rules, formal = fixture(tmp_path)
    merged, _ = merge_rule_findings(formal, rules, value)
    old = copy.deepcopy(merged['findings'][0]); old['finding_id'] = 'original-finding'
    old_evidence = [copy.deepcopy(e) for e in merged['evidences'] if e['finding_id']==merged['findings'][0]['finding_id']]
    for e in old_evidence:
        e['finding_id'] = old['finding_id']
    formal['findings'] = [old]; formal['evidences'] = old_evidence
    dedup, linked = merge_rule_findings(formal, rules, value)
    assert linked.decisions[0].finding_id == 'original-finding'
    assert len(dedup['findings']) == len(merged['findings'])
    old.update(title='另一项完全不同的风险', issue='另一问题', suggestion='另一建议')
    distinct, _ = merge_rule_findings(formal, rules, value)
    assert len(distinct['findings']) == len(merged['findings']) + 1


def test_result_validator_rejects_dangling_rule_card_link(tmp_path):
    value, rules, formal = fixture(tmp_path)
    merged, _ = merge_rule_findings(formal, rules, value)
    merged['rule_review']['decisions'][0]['finding_id'] = 'missing-card'
    with pytest.raises(ValueError, match='Finding'):
        validate(merged)
