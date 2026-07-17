from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PolicyGroupDefinition:
    code: str
    name: str
    keywords: tuple[str, ...]


POLICY_GROUPS = (
    PolicyGroupDefinition(
        "procurement_supply",
        "采购、招投标与供应商",
        ("采购", "招标", "投标", "供应商"),
    ),
    PolicyGroupDefinition(
        "contract_transaction",
        "合同与关联交易",
        ("合同", "关联交易"),
    ),
    PolicyGroupDefinition(
        "external_investment",
        "对外投资管理",
        ("对外投资", "投资管理"),
    ),
    PolicyGroupDefinition(
        "subsidiary_equity",
        "子公司与参股企业管理",
        ("参股企业", "子公司"),
    ),
    PolicyGroupDefinition(
        "financing_guarantee",
        "融资、担保与募集资金",
        ("融资", "担保", "募集资金"),
    ),
    PolicyGroupDefinition(
        "budget_expense",
        "预算与费用报销",
        ("预算", "费用报销"),
    ),
    PolicyGroupDefinition(
        "recruitment_employment",
        "招聘与劳动人事",
        ("招聘", "劳动人事"),
    ),
    PolicyGroupDefinition(
        "compensation_performance",
        "薪酬、绩效与奖惩",
        ("薪酬", "绩效", "奖惩"),
    ),
    PolicyGroupDefinition(
        "attendance_leave",
        "考勤与休假",
        ("考勤", "休假", "假期"),
    ),
    PolicyGroupDefinition(
        "digital_it",
        "信息化与IT资源",
        ("信息化", "IT资源", "信息技术"),
    ),
    PolicyGroupDefinition(
        "audit_control",
        "内部审计与内部控制",
        ("内部审计", "内部控制", "内控"),
    ),
    PolicyGroupDefinition(
        "corporate_governance",
        "董事会与会议治理",
        ("董事会", "会议管理", "议事规则"),
    ),
    PolicyGroupDefinition(
        "asset_administration",
        "资产与行政物资",
        ("固定资产", "车辆", "办公用品", "行政物资"),
    ),
)

POLICY_GROUP_NAMES = {
    definition.code: definition.name
    for definition in POLICY_GROUPS
} | {"other": "其他制度"}

_COARSE_GOVERNANCE_KEYWORDS = ("董事会", "子公司", "参股企业", "内部控制", "内部审计", "会议管理")
_COARSE_FINANCE_KEYWORDS = (
    "预算",
    "费用报销",
    "融资",
    "担保",
    "投资",
    "固定资产",
    "采购",
    "招标",
    "供应商",
    "合同",
)


def infer_policy_category(title: str) -> str:
    normalized = "".join(title.split()).upper()
    for definition in POLICY_GROUPS:
        if any(keyword.upper() in normalized for keyword in definition.keywords):
            return definition.code
    return "other"


def infer_coarse_policy_category(title: str) -> str:
    if any(keyword in title for keyword in _COARSE_GOVERNANCE_KEYWORDS):
        return "governance"
    if any(keyword in title for keyword in _COARSE_FINANCE_KEYWORDS):
        return "finance"
    return "general"


# Compatibility name for benchmark reports created while the fine taxonomy was
# still described as a policy group.
infer_policy_group = infer_policy_category
