"""Model-selected evidence, offline only: no provider/network calls."""
import asyncio
import copy
import json

import pytest

from services.contract.capabilities import risk_review_bundle as review
from services.contract.capabilities.risk_review import DirectReviewError
from test_contract_removed_limits import many_scope_sources
from test_contract_risk_base_bundle import (
    FakeRuntime, _completion, _po_candidate_payload, _po_catalog, _po_request,
    _icd_request, _icd_catalog,
)


def inspect_request(request):
    _, ir_refs, anchors = review._generic_prompt(request)
    candidates = review._build_generic_candidates(
        request, ir_refs, {item.anchor_id: ref for ref, item in anchors.items()})
    catalog = review._po_evidence_catalog(request, candidates, ir_refs, anchors)
    return candidates, catalog, ir_refs, anchors


def run_fake(request, *payloads):
    runtime = FakeRuntime([
        _completion(json.dumps(payload, ensure_ascii=False), request.unit_id, repair_no=index)
        for index, payload in enumerate(payloads)
    ])
    result = asyncio.run(review.GenericBaseDirectReviewer(runtime_factory=lambda _: runtime).review(
        request, tenant_id='offline', model_id='fake-model'))
    return result, runtime


def target_decision(payload, candidate):
    return next(item for item in payload['candidate_decisions']
                if item['candidate_id'] == candidate.candidate_id)


def scope_basis(candidate, catalog, count):
    directly_supported = next(source_id for source_id in candidate.primary_evidence_source_ids
        if review._po_factor_has_required_evidence('UNILATERAL_CONTROL',
            text_sources=[catalog.evidence_sources[source_id]], absence_sources=[])[0])
    return [directly_supported, *[source_id for source_id in candidate.primary_evidence_source_ids
                                 if source_id != directly_supported]][:count]


def test_named_input_keeps_all_26_materials_and_requires_explicit_output_primary():
    request = many_scope_sources(26)
    candidates, catalog, _, _ = inspect_request(request)
    frozen = [item.model_dump(mode='json') for item in candidates]
    prompt = json.loads(review._po_candidate_prompt(request, candidates, catalog).split('\n', 1)[1])
    assert 'candidate_legend' not in prompt
    from services.contract.capabilities.review_evidence_protocol import EVIDENCE_PROTOCOL_VERSION
    assert prompt['evidence_selection_contract']['version'] == EVIDENCE_PROTOCOL_VERSION
    target = next(item for check in prompt['assigned_checks'] for item in check['candidates']
                  if item['candidate_type'] == 'SCOPE_EXPANSION')
    assert len(target['trigger_material_source_ids']) == 26
    assert set(target['trigger_material_source_ids']) <= set(target['material_source_ids'])
    assert 'allowed_counter_evidence_source_ids' not in target
    for source_id in target['material_source_ids']:
        assert isinstance(prompt['source_catalog'][source_id], dict)
        assert prompt['source_catalog'][source_id]['quoted_text'] == catalog.evidence_sources[source_id].quoted_text
    assert 'primary_evidence_source_ids' in prompt['output_contract']['candidate_decision_fields']
    assert 'primary_evidence_source_ids' in review.CandidateDecisionRaw.model_json_schema()['required']
    assert [item.model_dump(mode='json') for item in candidates] == frozen


@pytest.mark.parametrize('selection_count', [1, 2, 26])
def test_only_selected_sources_reach_decision_root_finding_and_original_anchors(selection_count):
    request = many_scope_sources(26)
    candidates, catalog, _, _ = inspect_request(request)
    target = next(item for item in candidates if item.candidate_type == 'SCOPE_EXPANSION')
    selected = scope_basis(target, catalog, selection_count)
    payload = _po_candidate_payload(request, risk_check_code='PO-001', risk_candidate_type=target.candidate_type)
    target_decision(payload, target)['primary_evidence_source_ids'] = selected
    result, runtime = run_fake(request, payload)
    assert result.status == 'COMPLETED' and result.repair_count == 0
    decision = next(item for item in result.candidate_decisions if item.candidate_id == target.candidate_id)
    root = next(item for item in result.canonical_risk_roots if target.candidate_id in item.source_candidate_ids)
    finding = next(item for item in result.findings if item.finding_local_id == root.finding_local_id)
    assert decision.primary_evidence_source_ids == selected
    assert set(root.primary_evidence_source_ids) == set(selected)
    assert len(finding.evidence_candidates) == selection_count
    expected = {(catalog.evidence_sources[s].ir_item_id, catalog.evidence_sources[s].anchor_id,
                 catalog.evidence_sources[s].quoted_text_hash) for s in selected}
    assert {(e.source_ir_item_id, e.anchor_id, e.quoted_text_hash) for e in finding.evidence_candidates} == expected
    assert len(target.primary_evidence_source_ids) == 26  # never mutate recall/audit record
    assert len(runtime.calls) == 1


@pytest.mark.parametrize('role', ['Primary', 'Supporting', 'Counter'])
def test_source_use_is_selected_from_material_pool_not_historical_regex_role(role):
    request = _po_request()
    candidates, catalog, _, _ = inspect_request(request)
    candidate = next(item for item in candidates if item.candidate_type == 'ACCEPTANCE_MECHANISM_REVIEW')
    # A text source used to trigger review may also rebut the risk hypothesis.
    source_id = candidate.primary_evidence_source_ids[0]
    assert source_id in review._candidate_allowed_source_ids(candidate, role)
    assert review._validate_po_selected_sources([source_id], review._candidate_allowed_source_ids(candidate, role),
        candidate=candidate, role=role, catalog=catalog) == [source_id]


@pytest.mark.parametrize('role', ['Primary', 'Supporting', 'Counter'])
def test_shared_text_is_allowed_but_unknown_and_duplicate_selections_still_fail(role):
    request = _po_request()
    candidates, catalog, _, _ = inspect_request(request)
    candidate = next(item for item in candidates if item.candidate_type == 'ACCEPTANCE_MECHANISM_REVIEW')
    allowed = review._candidate_allowed_source_ids(candidate, role)
    assert set(catalog.evidence_sources) <= set(allowed)
    for selected, suffix in [(['risk-es-' + 'f' * 32], 'NOT_ALLOWED'),
                             ([allowed[0], allowed[0]], 'DUPLICATED')]:
        with pytest.raises(DirectReviewError) as exc:
            review._validate_po_selected_sources(selected, allowed, candidate=candidate, role=role, catalog=catalog)
        assert exc.value.code == f'RISK_{role.upper()}_EVIDENCE_{suffix}'


def test_old_shifted_output_cannot_silently_inherit_primary_and_has_bounded_repair():
    request = many_scope_sources(26)
    candidates, catalog, _, _ = inspect_request(request)
    target = next(item for item in candidates if item.candidate_type == 'SCOPE_EXPANSION')
    corrected = _po_candidate_payload(request, risk_check_code='PO-001', risk_candidate_type=target.candidate_type)
    selected = scope_basis(target, catalog, 1)
    target_decision(corrected, target)['primary_evidence_source_ids'] = selected
    shifted = copy.deepcopy(corrected)
    raw = target_decision(shifted, target)
    del raw['primary_evidence_source_ids']
    raw['supporting_evidence_source_ids'] = list(target.primary_evidence_source_ids)
    raw['counter_evidence_source_ids'] = list(target.allowed_supporting_evidence_source_ids)
    result, runtime = run_fake(request, shifted, corrected)
    assert result.status == 'COMPLETED' and len(runtime.calls) == 2
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert repair['task'] == 'EVIDENCE_SELECTION_REPAIR'
    assert repair['allowed_primary_evidence_source_ids_by_candidate'][target.candidate_id]
    assert set(target.primary_evidence_source_ids) <= set(repair['source_catalog'])
    assert repair['candidate_materials'][target.candidate_id]['material_source_ids']
    decision = next(item for item in result.candidate_decisions if item.candidate_id == target.candidate_id)
    assert decision.primary_evidence_source_ids == selected
    with pytest.raises(DirectReviewError) as exc:
        run_fake(request, shifted, shifted)
    assert exc.value.code == 'RISK_PRIMARY_EVIDENCE_MISSING'


def test_absence_cannot_become_counter_or_unselected_proof_of_missing_mechanism():
    request = _icd_request()
    candidates, catalog, _, _ = inspect_request(request)
    target = next(item for item in candidates if item.candidate_type == 'CONFIDENTIALITY_COMPLETENESS_ABSENT')
    absence = next(s for s in target.primary_evidence_source_ids if s.startswith('risk-as-'))
    assert absence in review._candidate_allowed_source_ids(target, 'Primary')
    assert absence not in review._candidate_allowed_source_ids(target, 'Counter')
    raw = review.CandidateDecisionRaw(candidate_id=target.candidate_id, verdict='RISK',
        primary_evidence_source_ids=[], decision_summary='缺少保密例外及期限。',
        severity_factors=['MISSING_CORE_MECHANISM'], recommended_control_codes=['ADD_CONFIDENTIALITY_EXCEPTIONS'])
    with pytest.raises(DirectReviewError) as exc:
        review._validate_po_candidate_verdict(target, raw, counter_ids=[],
            severity_factors=review._po_severity_factors(['MISSING_CORE_MECHANISM']))
    assert exc.value.code == 'RISK_PRIMARY_EVIDENCE_MISSING'


@pytest.mark.parametrize('malformed', [None, 'risk-es-not-an-array', [12], []])
def test_primary_format_or_empty_basis_can_be_repaired_without_an_immutable_field_trap(malformed):
    request = _po_request()
    corrected = _po_candidate_payload(request)
    first = copy.deepcopy(corrected)
    first['candidate_decisions'][0]['primary_evidence_source_ids'] = malformed
    result, runtime = run_fake(request, first, corrected)
    assert result.status == 'COMPLETED' and len(runtime.calls) == 2
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert repair['task'] == 'EVIDENCE_SELECTION_REPAIR'
    assert 'primary_evidence_source_ids' in ' '.join(repair['constraints'])


def test_no_risk_can_select_formerly_supplementary_text_as_primary_and_counter():
    request = _po_request()
    candidates, catalog, irs, anchors = inspect_request(request)
    target = next(item for item in candidates if item.candidate_type == 'ACCEPTANCE_MECHANISM_REVIEW')
    selected = next(s for s in target.allowed_supporting_evidence_source_ids if s in catalog.evidence_sources)
    raw = review.CandidateDecisionResponseRaw(candidate_decisions=[dict(
        candidate_id=target.candidate_id, verdict='NO_RISK', decision_summary='原文已约定对应的履行程序。',
        primary_evidence_source_ids=[selected], supporting_evidence_source_ids=[],
        counter_evidence_source_ids=[selected], severity_factors=[], recommended_control_codes=[])])
    _, findings, decisions, roots, *_ = review._materialize_po_candidate_decisions(
        request, raw, [target], catalog, irs, anchors)
    assert decisions[0].primary_evidence_source_ids == [selected]
    assert decisions[0].counter_evidence_source_ids == [selected]
    assert not findings and not roots


def test_unselected_trigger_cannot_supply_missing_severity_proof():
    request = many_scope_sources(26)
    candidates, catalog, _, _ = inspect_request(request)
    target = next(item for item in candidates if item.candidate_type == 'SCOPE_EXPANSION')
    unrelated = next(source_id for source_id in target.primary_evidence_source_ids
        if not review._po_factor_has_required_evidence('UNILATERAL_CONTROL',
            text_sources=[catalog.evidence_sources[source_id]], absence_sources=[])[0])
    payload = _po_candidate_payload(request, risk_check_code='PO-001', risk_candidate_type=target.candidate_type)
    target_decision(payload, target)['primary_evidence_source_ids'] = [unrelated]
    with pytest.raises(DirectReviewError) as exc:
        run_fake(request, payload, payload)
    assert exc.value.code == 'SEVERITY_FACTORS_INVALID'


def test_rejected_heuristic_factor_can_trigger_one_grounded_redecision():
    request = many_scope_sources(26)
    candidates, catalog, _, _ = inspect_request(request)
    target = next(c for c in candidates if c.candidate_type == 'SCOPE_EXPANSION')
    source = next(s for s in target.primary_evidence_source_ids if not review._po_factor_has_required_evidence(
        'UNILATERAL_CONTROL', text_sources=[catalog.evidence_sources[s]], absence_sources=[])[0])
    bad = _po_candidate_payload(request, risk_check_code='PO-001', risk_candidate_type=target.candidate_type)
    target_decision(bad, target)['primary_evidence_source_ids'] = [source]
    corrected = copy.deepcopy(bad)
    target_decision(corrected, target).update(verdict='NO_RISK',
        decision_summary='该段为固定交付数量，不能证明存在单方扩大范围的权利。',
        primary_evidence_source_ids=[source], counter_evidence_source_ids=[source],
        supporting_evidence_source_ids=[], severity_factors=[], recommended_control_codes=[])
    result, runtime = run_fake(request, bad, corrected)
    assert result.status == 'COMPLETED' and len(runtime.calls) == 2
    repair = json.loads(runtime.calls[1]['messages'][0]['content'])
    assert repair['task'] == 'DECISION_SUPPORT_REPAIR'
    assert target.candidate_id in repair['target_candidate_ids']
    assert 'rejected factors' in repair['exact_validation_error']
    assert not any(target.candidate_id in root.source_candidate_ids for root in result.canonical_risk_roots)


def test_insufficient_evidence_can_remain_empty_without_fabricating_primary():
    request = _po_request()
    payload = _po_candidate_payload(request)
    raw = payload['candidate_decisions'][0]
    raw.update(verdict='INSUFFICIENT_EVIDENCE', primary_evidence_source_ids=[],
        supporting_evidence_source_ids=[], counter_evidence_source_ids=[], severity_factors=[],
        recommended_control_codes=[], decision_summary='现有材料不能证明该事实或其反面，需补充完整附件。')
    result, runtime = run_fake(request, payload, payload)
    decision = next(item for item in result.candidate_decisions if item.candidate_id == raw['candidate_id'])
    assert decision.primary_evidence_source_ids == []
    assert result.status == 'PARTIAL_FAILED' and len(runtime.calls) == 2
    assert not any(raw['candidate_id'] in root.source_candidate_ids for root in result.canonical_risk_roots)
