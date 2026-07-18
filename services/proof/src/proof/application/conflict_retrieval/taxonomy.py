from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PolicyCategoryDefinition:
    code: str
    name: str
    parent_code: str
    keywords: tuple[str, ...]


PARENT_CATEGORY_NAMES = {
    "governance_oversight": "公司治理与监督",
    "capital_finance": "投融资与资本",
    "procurement_transactions": "采购与交易",
    "finance_assets": "财务与资产",
    "human_resources": "人力资源",
    "digital_operations": "数字化与运营",
}

POLICY_CATEGORIES = (
    PolicyCategoryDefinition(
        "procurement_supply",
        "采购、招投标与供应商",
        "procurement_transactions",
        ("采购", "招标", "投标", "供应商"),
    ),
    PolicyCategoryDefinition(
        "contract_transaction",
        "合同与关联交易",
        "procurement_transactions",
        ("合同", "关联交易"),
    ),
    PolicyCategoryDefinition(
        "external_investment",
        "对外投资管理",
        "capital_finance",
        ("对外投资", "投资管理"),
    ),
    PolicyCategoryDefinition(
        "subsidiary_equity",
        "子公司与参股企业管理",
        "governance_oversight",
        ("参股企业", "子公司"),
    ),
    PolicyCategoryDefinition(
        "financing_guarantee",
        "融资、担保与募集资金",
        "capital_finance",
        ("融资", "担保", "募集资金"),
    ),
    PolicyCategoryDefinition(
        "budget_expense",
        "预算与费用报销",
        "finance_assets",
        ("预算", "费用报销"),
    ),
    PolicyCategoryDefinition(
        "recruitment_employment",
        "招聘与劳动人事",
        "human_resources",
        ("招聘", "劳动人事"),
    ),
    PolicyCategoryDefinition(
        "compensation_performance",
        "薪酬、绩效与奖惩",
        "human_resources",
        ("薪酬", "绩效", "奖惩"),
    ),
    PolicyCategoryDefinition(
        "attendance_leave",
        "考勤与休假",
        "human_resources",
        ("考勤", "休假", "假期"),
    ),
    PolicyCategoryDefinition(
        "digital_it",
        "信息化与IT资源",
        "digital_operations",
        ("信息化", "IT资源", "信息技术"),
    ),
    PolicyCategoryDefinition(
        "audit_control",
        "内部审计与内部控制",
        "governance_oversight",
        ("内部审计", "内部控制", "内控"),
    ),
    PolicyCategoryDefinition(
        "corporate_governance",
        "董事会与会议治理",
        "governance_oversight",
        ("董事会", "会议管理", "议事规则"),
    ),
    PolicyCategoryDefinition(
        "asset_administration",
        "资产与行政物资",
        "finance_assets",
        ("固定资产", "车辆", "办公用品", "行政物资"),
    ),
)

LEAF_CATEGORY_NAMES = {
    definition.code: definition.name
    for definition in POLICY_CATEGORIES
} | {"other": "其他制度"}

CATEGORY_PARENT_BY_CODE = {
    definition.code: definition.parent_code
    for definition in POLICY_CATEGORIES
}

CHILD_CATEGORY_CODES = {
    parent_code: tuple(
        definition.code
        for definition in POLICY_CATEGORIES
        if definition.parent_code == parent_code
    )
    for parent_code in PARENT_CATEGORY_NAMES
}

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
    for definition in POLICY_CATEGORIES:
        if any(keyword.upper() in normalized for keyword in definition.keywords):
            return definition.code
    return "other"


def infer_coarse_policy_category(title: str) -> str:
    if any(keyword in title for keyword in _COARSE_GOVERNANCE_KEYWORDS):
        return "governance"
    if any(keyword in title for keyword in _COARSE_FINANCE_KEYWORDS):
        return "finance"
    return "general"
