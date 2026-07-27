"""Shared deterministic PO Evidence Source applicability rules."""

from __future__ import annotations

import re
from collections.abc import Iterable

from contract.risk.models import CheckSpec, RiskProjectedIrItem, RiskSourceExcerpt


PO_SOURCE_PATTERN_RULES: dict[
    str,
    tuple[tuple[str, str, tuple[str, ...]], ...],
] = {
    "PO-001": (
        (
            "SCOPE_EXPANSION",
            "存在开放式、单方或未封闭的服务/交付范围表述",
            (
                r"按.{0,10}要求",
                r"提出.{0,8}要求",
                r"其他要求",
                r"包括但不限于",
                r"随时.{0,8}(增加|扩大|调整)",
                r"单方.{0,8}(增加|扩大|调整)",
                r"尽量满足",
            ),
        ),
        (
            "DELIVERY_SCHEDULE_REVIEW",
            "存在交付、完成或进度表述，需要核对内容、期限和责任边界",
            (r"交付", r"完成.{0,8}(项目|任务|服务|工作)", r"进度", r"工期"),
        ),
    ),
    "PO-002": (
        (
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "存在单方审批、检查、解释、拒绝或控制性权利，需要核对对等保护",
            (
                r"单方",
                r"自行",
                r"随时",
                r"有权.{0,12}(检查|决定|拒绝|暂停|要求)",
                r"最终解释",
                r"须按.{0,8}要求",
            ),
        ),
    ),
    "PO-003": (
        (
            "COOPERATION_DEPENDENCY",
            "存在配合、资料、接口、前置条件或延迟依赖，需要核对责任和顺延",
            (r"配合", r"协助", r"提供.{0,8}(资料|材料|接口|条件)", r"前置", r"依赖", r"延迟"),
        ),
    ),
    "PO-004": (
        (
            "QUALITY_STANDARD_UNMEASURABLE",
            "存在可能不可衡量的质量或服务标准，需要核对客观指标",
            (
                r"尽量",
                r"满足.{0,8}要求",
                r"达到.{0,8}目的",
                r"适当",
                r"专业.{0,6}水准",
                r"行业.{0,6}(规范|标准)",
            ),
        ),
        (
            "ACCEPTANCE_MECHANISM_REVIEW",
            "存在验收或确认表述，需要核对标准、期限、程序、整改和复验",
            (r"验收", r"视为.{0,6}(验收|合格)", r"确认.{0,8}(合格|完成)", r"复验"),
        ),
    ),
    "PO-005": (
        (
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "存在第三方、委托、分包或转让表述，需要核对同意和持续责任",
            (r"转委托", r"分包", r"转让", r"第三方", r"委托", r"债权", r"债务"),
        ),
    ),
    "PO-006": (
        (
            "CHANGE_CONTROL_REVIEW",
            "存在需求、范围、工期或价款调整线索，需要核对书面变更和联动机制",
            (
                r"变更",
                r"调整",
                r"新增",
                r"额外",
                r"提出.{0,8}要求",
                r"其他要求",
                r"按.{0,8}要求",
                r"书面确认",
            ),
        ),
    ),
    "PO-007": (
        (
            "WARRANTY_SUPPORT_REVIEW",
            "存在质保、维护、整改、修复、响应或支持线索，需要核对范围、期限和复验",
            (r"质保", r"维护", r"维修", r"整改", r"修复", r"支持", r"响应", r"解决方案", r"复验"),
        ),
    ),
}


def po_item_matches_check(
    item: RiskProjectedIrItem,
    excerpts: Iterable[RiskSourceExcerpt],
    check: CheckSpec,
) -> bool:
    """Return whether one complete IR item belongs to a PO Check source pool."""
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
        for _candidate_type, _reason, patterns in PO_SOURCE_PATTERN_RULES[
            check.check_code
        ]
        for pattern in patterns
    )
