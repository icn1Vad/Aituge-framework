"""Deterministic LRE Evidence Source and mechanism-completeness policies."""

from __future__ import annotations

import re
from collections.abc import Iterable

from contract.risk.models import (
    CheckSpec,
    RiskEvidenceSource,
    RiskProjectedIrItem,
    RiskSourceExcerpt,
)


LRE_SOURCE_PATTERN_RULES: dict[str, tuple[str, ...]] = {
    "LRE-001": (
        r"重大违约",
        r"违约责任",
        r"违反.{0,18}(约定|合同)",
        r"违约情形",
    ),
    "LRE-002": (
        r"违约金",
        r"损失赔偿",
        r"直接损失",
        r"间接损失",
        r"可预期利益",
        r"同时.{0,8}主张",
    ),
    "LRE-003": (
        r"责任上限",
        r"责任限额",
        r"全部赔偿",
        r"间接损失",
        r"可预期利益",
        r"同时.{0,8}主张",
    ),
    "LRE-004": (
        r"赔偿",
        r"补偿",
        r"索赔",
        r"第三方",
        r"律师费",
        r"全部责任",
    ),
    "LRE-005": (
        r"解除",
        r"终止",
        r"整改",
        r"改正",
    ),
    "LRE-006": (
        r"解除",
        r"终止",
        r"结算",
        r"归还",
        r"返还",
        r"恢复原状",
    ),
    "LRE-007": (
        r"不可抗力",
        r"情势变更",
        r"情势变化",
        r"风险分配",
    ),
    "LRE-008": (
        r"适用法律",
        r"争议解决",
        r"管辖",
        r"仲裁机构",
        r"人民法院",
        r"提交仲裁",
        r"协商不成",
    ),
}


LRE_ABSENCE_POLICIES: dict[str, tuple[str, str, str]] = {
    "LRE-003": (
        "全部违约责任、损害赔偿、违约金、退款及其他金钱责任的责任总上限、计算基数和例外",
        "Python扫描当前Batch全部责任、赔偿、违约金和金额Source，核对是否存在可适用于开放损失条款的责任总上限、计算基数和封闭例外",
        "责任总上限、计算基数及例外边界",
    ),
    "LRE-006": (
        "解除或终止后的费用结算、物品资料返还、已完成工作处理和持续义务",
        "Python扫描当前Batch全部解除、终止、付款、结算、返还和持续义务Source，核对退出后的处理闭环",
        "终止后的结算、返还和持续义务闭环",
    ),
    "LRE-007": (
        "不可抗力的定义、通知、证明、减损、持续期间、费用进度后果及解除安排",
        "Python扫描当前Batch全部责任和终止Source，核对是否存在完整不可抗力及其风险分配机制",
        "不可抗力定义、通知、减损、履行后果和退出机制",
    ),
    "LRE-008": (
        "适用法律、单一明确的诉讼或仲裁路径、法院或仲裁机构及前置协商期限",
        "Python扫描当前Batch全部争议解决Source；仅出现诉讼费或仲裁费不构成争议解决机制",
        "适用法律及明确、无冲突的争议解决和管辖机制",
    ),
}


LRE_BROAD_BREACH_TRIGGER_PATTERN = (
    r"((任何|任一|全部|所有|一切).{0,16}(违反|违约)"
    r"|(?:违反|未履行).{0,10}(?:本合同|本协议).{0,10}"
    r"(?:任何|任一|全部|所有|约定|条款).{0,14}(?:重大违约|解除|赔偿|责任)"
    r"|(?:由|可由).{0,8}(?:一方|对方|甲方|乙方).{0,12}"
    r"(?:认定|判断|认为).{0,12}(?:重大违约|违约)"
    r"|(?:一方|对方|甲方|乙方).{0,8}(?:可自行|自行|有权).{0,8}"
    r"(?:认定|判断|认为).{0,12}(?:重大违约|违约)"
    r"|(?:轻微|一般|任意|其他).{0,12}(?:重大违约|违约)"
    r"|(?:不满意|未达到要求|不符合要求).{0,12}(?:重大违约|违约))"
)


def lre_has_broad_breach_trigger(text: str) -> bool:
    """Require a self-contained broad or subjective breach trigger."""
    return bool(re.search(LRE_BROAD_BREACH_TRIGGER_PATTERN, text))


_LRE_COMPLETENESS_SIGNAL_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "LRE-003": (
        (r"(责任|赔偿|违约金).{0,20}(上限|限额|最高|不超过)",),
        (r"(上限|限额).{0,20}(合同价款|已付费用|年度费用|人民币|元)",),
    ),
    "LRE-006": (
        (r"(解除|终止).{0,18}(结算|支付|费用)", r"(结算|支付).{0,18}(解除|终止)"),
        (r"(解除|终止).{0,24}(归还|返还|退还)", r"(归还|返还|退还).{0,18}(物品|设备|资料|数据|载体)"),
    ),
    "LRE-007": (
        (r"不可抗力",),
        (r"(不可抗力).{0,24}(通知|证明)", r"(通知|证明).{0,24}不可抗力"),
        (r"(不可抗力).{0,30}(减损|履行|延期|顺延|费用|解除|终止)",),
    ),
    "LRE-008": (
        (r"(适用|依据).{0,12}(中华人民共和国)?法律",),
        (
            r"(争议|纠纷).{0,24}(人民法院|法院|仲裁)",
            r"(人民法院|法院|仲裁).{0,24}(争议|纠纷)",
        ),
        (r"(管辖|仲裁委员会|仲裁机构|人民法院)",),
    ),
}


def lre_item_matches_check(
    item: RiskProjectedIrItem,
    excerpts: Iterable[RiskSourceExcerpt],
    check: CheckSpec,
) -> bool:
    """Return whether a complete IR item belongs to one LRE Check pool."""
    if item.ir_type not in check.required_ir_types:
        return False
    text = "\n".join(
        [
            item.subject or "",
            item.predicate,
            item.object or "",
            *(excerpt.quoted_text for excerpt in excerpts),
        ]
    )
    return any(
        re.search(pattern, text)
        for pattern in LRE_SOURCE_PATTERN_RULES[check.check_code]
    )


def lre_mechanism_is_complete(
    check_code: str,
    sources: Iterable[RiskEvidenceSource],
) -> bool:
    """Conservatively prove every frozen LRE mechanism group is present."""
    groups = _LRE_COMPLETENESS_SIGNAL_GROUPS.get(check_code)
    if groups is None:
        return False
    text = "\n".join(
        " ".join(
            filter(
                None,
                (
                    source.subject,
                    source.predicate,
                    source.object,
                    source.quoted_text,
                ),
            )
        )
        for source in sources
        if check_code in source.allowed_check_codes
    )
    return bool(text) and all(
        any(re.search(pattern, text) for pattern in alternatives)
        for alternatives in groups
    )
