"""Project grounded rule risks into the existing persisted Finding/card path.

No model call; no display-only shadow cards. No-risk and uncertain decisions are
retained in rule_review but never invented into Findings. Preview rule risks are
review suggestions, not declarations that a contract is unlawful.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


def _id(prefix, *values):
    return prefix + hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def _text(value):
    return re.sub(r'[\s，。；：,.!?！？]', '', value or '')


def merge_rule_findings(formal, rule_result, value):
    from contract.risk.plan_builder import RiskReviewPlanBuilder
    from contract.evidence_planning.review_result import RuleReviewResult

    result = copy.deepcopy(formal)
    rules = rule_result.model_copy(deep=True)
    if rules.review_id != value.review_id or rules.generation_id != value.generation_id:
        raise ValueError('Rule Finding projection belongs to another review/generation')
    profile = result['contract_profile']
    perspective = str(getattr(value.perspective, 'value', value.perspective))
    if rules.perspective != perspective or profile['perspective'] != perspective:
        raise ValueError('Rule Finding projection perspective mismatch')
    blocks = {block.block_id: block for block in value.source_blocks}
    plan = RiskReviewPlanBuilder().build(value)
    specs = {spec.check_code: spec for ctx in plan.contexts for spec in ctx.check_specs}
    basis = {item.evidence_id: item for item in rules.evidence}
    findings, evidence = result['findings'], result['evidences']

    for decision in rules.decisions:
        if decision.outcome != 'RISK':
            continue
        rule = basis[decision.evidence_id]
        categories = [category for code in rule.check_codes if code in specs for category in specs[code].allowed_categories]
        category = categories[0] if categories else 'OTHER'
        quotes = []
        for citation in decision.citations:
            block = blocks.get(citation.block_id)
            if block is None or block.text[citation.char_start:citation.char_end] != citation.quoted_text:
                raise ValueError('Rule Finding citation is not the frozen contract original')
            quotes.append(dict(evidence_type='TEXT_QUOTE', block_id=citation.block_id,
                page_number=block.page_number, char_start=citation.char_start, char_end=citation.char_end,
                quoted_text=citation.quoted_text, quoted_text_hash=citation.quoted_text_hash,
                checked_scope=None, verification_note=None, bounding_boxes=[]))
        if not quotes:
            raise ValueError('A rule risk cannot become a Finding without original text')
        fid = _id('finding-', 'rule-projection-v1', rules.input_hash, decision.decision_id)
        existing = next((f for f in findings if f['finding_id'] == fid), None)
        if existing is None:
            # Conservative dedup: shared location alone is not the same risk.
            # Require identical reasoning plus title or recommendation too.
            locations = {(q['block_id'],q['char_start'],q['char_end']) for q in quotes}
            for item in findings:
                same_reason = _text(item['issue']) == _text(decision.reason) and (
                    _text(item['title']) == _text(decision.title) or _text(item['suggestion']) == _text(decision.suggestion))
                item_locations = {(e.get('block_id'),e.get('char_start'),e.get('char_end')) for e in evidence if e['finding_id']==item['finding_id']}
                if item['category']==category and same_reason and locations & item_locations:
                    existing = item
                    fid = item['finding_id']
                    break
        if existing is None:
            existing = dict(finding_id=fid, category=category, risk_level='MEDIUM',
                title=decision.title, perspective=perspective, our_party=profile['our_party'], counterparty=profile['counterparty'],
                issue=decision.reason, impact_to_our_party=decision.reason, suggestion=decision.suggestion,
                evidence_ids=[], legal_evidence_ids=[])
            findings.append(existing)
        for quote in quotes:
            eid = _id('evidence-', fid, quote['block_id'], quote['char_start'], quote['char_end'], quote['quoted_text_hash'])
            duplicate = next((e for e in evidence if e['finding_id']==fid and all(e.get(k)==quote[k] for k in ('block_id','char_start','char_end','quoted_text_hash'))), None)
            if duplicate is not None:
                eid = duplicate['evidence_id']
            else:
                evidence.append(dict(evidence_id=eid, finding_id=fid, **quote))
            if eid not in existing['evidence_ids']:
                existing['evidence_ids'].append(eid)
        decision.finding_id = fid

    for level in ('HIGH','MEDIUM','LOW','INFO'):
        result['summary'][level.lower()+'_count'] = sum(f['risk_level']==level for f in findings)
    # Do not carry a pre-merge hash into the unchanged final result validator.
    result.pop('result_hash', None)
    rules = RuleReviewResult.model_validate(rules.model_dump())
    result['rule_review'] = rules.model_dump(mode='json')
    return result, rules
