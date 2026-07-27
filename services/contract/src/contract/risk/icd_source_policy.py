"""Deterministic ICD Evidence Source applicability and absence policies."""

from __future__ import annotations

import re
from collections.abc import Iterable

from contract.risk.models import (
    CheckSpec,
    RiskEvidenceSource,
    RiskProjectedIrItem,
    RiskSourceExcerpt,
)


ICD_SOURCE_PATTERN_RULES: dict[
    str,
    tuple[tuple[str, str, tuple[str, ...]], ...],
] = {
    "ICD-001": (
        (
            "FOREGROUND_IP_OWNERSHIP_REVIEW",
            "存在项目成果、新增知识产权或交付成果权属表述，需要核对归属和使用边界",
            (
                r"(项目|工作|服务|交付).{0,12}(成果|作品|软件|文档).{0,18}(知识产权|著作权|所有权|归属)",
                r"(成果|作品|软件|源代码|文档).{0,12}(知识产权|著作权|所有权|归属)",
                r"(知识产权|著作权).{0,10}(归|属于|所有)",
            ),
        ),
    ),
    "ICD-002": (
        (
            "BACKGROUND_IP_LICENSE_REVIEW",
            "存在背景知识产权或许可表述，需要核对范围、期限、地域、转授权和终止后使用",
            (
                r"(背景|原有|既有|预先存在).{0,12}(知识产权|技术|软件|资料|成果)",
                r"(知识产权|软件|技术|源代码|资料).{0,12}(许可|授权使用|使用权|转授权)",
                r"(许可|授权使用).{0,18}(期限|地域|范围|用途|转授权|不可撤销|永久)",
            ),
        ),
    ),
    "ICD-003": (
        (
            "THIRD_PARTY_IP_PROTECTION_REVIEW",
            "存在第三方知识产权、不侵权或权利索赔表述，需要核对保证和救济闭环",
            (
                r"第三方.{0,15}(知识产权|著作权|专利|商标|权利|侵权)",
                r"(不侵犯|不侵害|无权利瑕疵|权利保证)",
                r"(侵权|权利索赔).{0,20}(抗辩|处理|替换|修改|许可|赔偿|补偿)",
            ),
        ),
    ),
    "ICD-004": (
        (
            "CONFIDENTIALITY_PROTECTION_REVIEW",
            "存在保密表述，需要核对信息范围、例外、允许披露对象、期限和法定披露程序",
            (
                r"保密",
                r"商业秘密",
                r"机密",
                r"秘密信息",
                r"(披露|泄露).{0,12}(信息|资料|秘密)",
            ),
        ),
    ),
    "ICD-005": (
        (
            "DATA_PROCESSING_SECURITY_REVIEW",
            "存在数据或信息处理表述，需要核对目的、范围、访问、安全措施和事件响应",
            (
                r"(数据|个人信息|业务信息).{0,15}(收集|使用|处理|访问|传输|共享|安全|泄露|事件)",
                r"(收集|使用|处理|访问|传输|共享).{0,12}(数据|个人信息|业务信息)",
                r"(安全措施|访问控制|安全事件|数据泄露|事件通知)",
            ),
        ),
    ),
    "ICD-006": (
        (
            "DATA_RETURN_DELETION_REVIEW",
            "存在数据或资料权属、返还、删除、销毁、备份或留存表述，需要核对终止后闭环",
            (
                r"(数据|资料|信息|载体).{0,15}(权属|归属|返还|退还|删除|销毁|留存|备份)",
                r"(返还|退还|删除|销毁|留存).{0,12}(数据|资料|信息|载体)",
                r"(合同终止|合作结束|服务结束).{0,18}(返还|删除|销毁|留存)",
            ),
        ),
    ),
}


ICD_ABSENCE_POLICIES: dict[str, tuple[str, str, str]] = {
    "ICD-001": (
        "项目成果、新增作品、软件、文档及其他交付成果的知识产权归属与双方使用边界",
        "按ICD-001领域词形和IR类型扫描知识产权、权利及义务Source；未定位到成果权属或使用边界的完整机制",
        "项目成果和新增知识产权归属机制",
    ),
    "ICD-002": (
        "双方背景知识产权、既有技术和软件的保留权利及许可范围、期限、地域、用途、转授权与终止后使用",
        "按ICD-002领域词形和IR类型扫描知识产权及权利Source；未定位到背景知识产权许可边界的完整机制",
        "背景知识产权保留和许可边界机制",
    ),
    "ICD-003": (
        "第三方知识产权保证、不侵权承诺、索赔通知、抗辩控制、替换修改和赔偿救济",
        "按ICD-003领域词形和IR类型扫描知识产权及责任Source；未定位到第三方权利保证和救济闭环",
        "第三方知识产权保证和侵权救济机制",
    ),
    "ICD-004": (
        "保密信息定义、例外、允许披露对象、法定披露程序、保护措施和保密期限",
        "按ICD-004领域词形和IR类型扫描保密、期限及义务Source；未定位到完整保密保护机制",
        "完整保密范围、例外、披露程序和期限机制",
    ),
    "ICD-005": (
        "数据使用目的、处理范围、访问权限、安全标准、安全事件响应和通知责任",
        "按ICD-005领域词形和IR类型扫描数据、信息安全及义务Source；未定位到数据处理与安全机制",
        "数据处理、访问、安全标准和事件通知机制",
    ),
    "ICD-006": (
        "数据及载体权属、合同终止或服务结束后的返还、删除、销毁、备份和法定留存",
        "按ICD-006领域词形和IR类型扫描数据、资料、权利及义务Source；未定位到数据返还删除留存闭环",
        "数据权属、返还、删除和留存机制",
    ),
}


def icd_item_matches_check(
    item: RiskProjectedIrItem,
    excerpts: Iterable[RiskSourceExcerpt],
    check: CheckSpec,
) -> bool:
    """Return whether one complete IR item belongs to an ICD Check source pool."""
    if item.ir_type not in check.required_ir_types:
        return False
    if check.check_code not in ICD_SOURCE_PATTERN_RULES:
        # Extension playbooks may add checks to the ICD domain. Their own
        # required_ir_types remain authoritative until they freeze a dedicated
        # ICD source-pattern policy.
        return True
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
        for _candidate_type, _reason, patterns in ICD_SOURCE_PATTERN_RULES[
            check.check_code
        ]
        for pattern in patterns
    )


_ICD_COMPLETENESS_SIGNAL_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ICD-001": (
        (r"(项目|新增|交付).{0,12}(成果|作品|软件|文档)",),
        (r"(知识产权|著作权).{0,12}(归属|属于|所有)",),
        (r"(使用|许可|保留).{0,12}(范围|目的|权利|边界)",),
    ),
    "ICD-002": (
        (r"(背景|原有|既有|预先存在|自有).{0,12}(知识产权|技术|软件|模板|方法|工具)",),
        (r"(仍归|继续归|保留).{0,12}(原权利人|一方|甲方|乙方|所有)",),
        (r"(许可|授权使用).{0,12}(目的|范围|期限|用途)",),
    ),
    "ICD-003": (
        (r"(第三方|不侵权|不侵犯|权利瑕疵)",),
        (r"(保证|承诺|担保)",),
        (r"(抗辩|替换|修改|取得许可|赔偿|补偿|救济)",),
    ),
    "ICD-004": (
        (r"(保密信息|商业秘密|秘密信息).{0,12}(包括|范围|定义|是指)",),
        (r"(已公开|在先持有|独立开发|依法披露|法定披露)",),
        (r"(保密期限|保密义务).{0,12}(年|期限|终止后|持续)",),
        (r"(员工|关联方|顾问|依法).{0,12}(披露|知悉|通知|保密)",),
    ),
    "ICD-005": (
        (r"(数据|个人信息|业务信息).{0,12}(目的|范围|仅用于|限于)",),
        (r"(访问控制|最小权限|加密|安全措施|安全标准)",),
        (r"(安全事件|数据泄露).{0,12}(通知|报告|响应|处置)",),
    ),
    "ICD-006": (
        (r"(终止|结束|到期).{0,12}(返还|删除|销毁|导出)",),
        (r"(数据|资料|载体).{0,12}(返还|删除|销毁)",),
        (r"(备份|留存).{0,12}(期限|法定|删除|销毁|例外)",),
    ),
}


def icd_mechanism_is_complete(
    check_code: str,
    sources: Iterable[RiskEvidenceSource],
) -> bool:
    """Conservatively prove that every frozen mechanism group is present."""
    if check_code not in _ICD_COMPLETENESS_SIGNAL_GROUPS:
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
    if not text:
        return False
    return all(
        any(re.search(pattern, text) for pattern in alternatives)
        for alternatives in _ICD_COMPLETENESS_SIGNAL_GROUPS[check_code]
    )
