"""Conservative, source-local payment observations, never legal conclusions.

Each payment event owns its timing and payer. Unknown syntax stays UNKNOWN;
another IR fact sharing the paragraph is not authority for the payer.
"""
from __future__ import annotations

import re

PAYMENT = re.compile(r'预付|支付|付款|付清|付给|给付')
PENALTY = re.compile(r'违约金|赔偿|罚款|滞纳金|利息|损失')
NEGATION = re.compile(r'无需|不得|不必|不应|未提供|未约定|未设|未见|没有|不提供|不设|不予|无须|免于|免除|无')
AFTER = re.compile(
    r'(?:验收(?:合格|通过|完成)?|完成验收(?:工作)?|通过验收|交付(?:完成)?|交货|履约完成|服务完成)'
    r'(?:之日)?(?:起|后)?\s*(?:[0-9０-９一二三四五六七八九十百]+\s*个?\s*(?:工作日|日|天|月|年))?'
    r'(?:之后|以后|后|内)')
BEFORE = re.compile(r'签订后|合同签订|签订合同|生效后|合同生效|预付')


def _payment_heading_or_currency(segment: str) -> bool:
    """Skip field labels, not payment obligations with unknown conditions."""
    text = re.sub(r'\s+', '', segment).rstrip('：:')
    if re.search(r'(?:按照|按|采用|以)?(?:以下|下列|如下)方式(?:支付|付款)$', text):
        return True
    if re.fullmatch(r'(?:[\d.、（）()]+)?(?:第[一二三四五六七八九十]+条)?(?:付款|支付)(?:方式|条件|币种|安排|账户)', text):
        return True
    return bool(re.fullmatch(r'(?:本合同|合同价款|合同款项)?(?:以|采用)(?:人民币|美元|欧元|港币|日元)(?:支付|付款)(?:[（(]单位[:：]?[^）)]+[）)])?', text))


def affirmative(text: str, word: str) -> bool:
    """A negated/blank mention cannot establish a safeguard or obligation."""
    for match in re.finditer(re.escape(word), text):
        prefix = re.split(r'[，,。；;\n]|但是|但', text[:match.start()])[-1]
        suffix = re.split(r'[，,。；;\n]', text[match.end():])[0]
        if NEGATION.search(prefix):
            continue
        if re.match(r'\s*[:：为是]?\s*[_＿]+', suffix):
            continue
        if re.match(r'\s*[:：为是]?\s*(?:不适用|无|未提供|未约定)', suffix):
            continue
        return True
    return False


def payment_events(text: str, roles) -> list[dict]:
    aliases = [(alias, status) for status, names in (
        ('OUR_PARTY', roles.aliases_for_our_party()), ('COUNTERPARTY', roles.aliases_for_counterparty()))
        for alias in names if alias]
    # Prefer complete names over a shorter overlapping alias.
    aliases.sort(key=lambda item: len(item[0]), reverse=True)
    events = []
    for sentence in re.split(r'[。；;\n]', text):
        inherited_payer = 'AMBIGUOUS'
        pending = ''
        for segment in re.split(r'[，,]', sentence):
            verbs = list(PAYMENT.finditer(segment))
            if not verbs:
                pending = (pending + '，' + segment).strip('，')
                continue
            event_text = (pending + '，' + segment).strip('，')
            pending = ''
            # Price and breach-payment clauses are separate events. A penalty
            # in a following segment never deletes the earlier price event.
            if PENALTY.search(segment) and not re.search(r'预付|价款|货款|合同总价', segment):
                continue
            if _payment_heading_or_currency(segment):
                continue
            # "不再另行支付" describes exclusion of an extra charge, not a
            # separate price instalment with an unknown payment deadline.
            if all(re.search(r'不(?:再)?(?:另行|另外)?\s*$', segment[:m.start()]) for m in verbs):
                continue
            if all(not affirmative(segment, m.group()) for m in verbs):
                continue
            prefix = event_text[:event_text.rfind(segment) + verbs[0].start()]
            mentions = []
            covered = set()
            for alias, status in aliases:
                for m in re.finditer(re.escape(alias), prefix):
                    positions = set(range(m.start(), m.end()))
                    if positions & covered:
                        continue
                    covered.update(positions)
                    mentions.append((m.start(), m.end(), status))
            mentions.sort()
            payer = inherited_payer
            if mentions:
                chosen = mentions[-1]
                if len(mentions)>1 and '向' in prefix[mentions[-2][1]:chosen[0]]:
                    chosen = mentions[-2]  # payer 向 recipient 支付
                payer = chosen[2]
                inherited_payer = payer
            before = bool(BEFORE.search(event_text))
            after = bool(AFTER.search(event_text))
            # Conflicting conditions within ONE event are unresolved, not
            # silently collapsed. Distinct comma-separated payments stay apart.
            timing = ('UNKNOWN' if '预付' in segment else 'AFTER') if before and after else 'BEFORE' if before else 'AFTER' if after else 'UNKNOWN'
            percentages = [float(v) for v in re.findall(r'(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%', segment)]
            large = any(70 <= v <= 100 for v in percentages) or bool(re.search(r'全额|全部|百分之百|一次性|绝大部分', segment))
            events.append(dict(text=event_text, payer_role_status=payer, timing=timing, large_payment_share=large))
    return events
