from __future__ import annotations

import re
from typing import Any


# These values require an external registry, policy list, administrative mapping,
# or group master data.  The document-only workflow must not guess them.
EXTERNAL_DATA_FIELD_IDS = frozenset({
    "national_economy_industry_category",
    "national_economy_industry_code",
    "belt_road_country",
    "is_state_capital_project",
    "target_company_source",
    "target_company_management_level",
    "target_company_property_level",
    "target_company_district_county",
})

FORECAST_COLUMNS = ["投资年份（t0）", *[f"t0+{index}" for index in range(1, 8)]]
FORECAST_CATEGORY_ALIASES = {
    "营业收入（万元）": "营业收入",
    "营业收入(万元)": "营业收入",
    "营收合计": "营业收入",
    "净利润-合并（万元）": "合并净利润",
    "净利润-合并(万元)": "合并净利润",
    "净利润合计": "合并净利润",
    "合并净利润（万元）": "合并净利润",
    "归母净利润（万元）": "归母净利润",
    "归母净利润(万元)": "归母净利润",
    "归属于航天氢能净利润": "归母净利润",
    "营业成本（万元）": "营业成本",
    "利润总额（万元）": "利润总额",
}
MAX_LENGTH_BY_FIELD_ID = {
    "project_name": 100,
    "project_background": 300,
    "project_content": 300,
    "main_business_summary": 1000,
    "financial_summary": 1000,
    "performance_indicator_summary": 1000,
    "profit_forecast": 1000,
    "financial_evaluation": 1000,
    "major_competitors": 300,
    "competitive_advantage": 300,
    "competitive_disadvantage": 300,
}
DISCLOSURE_REQUIRED_NEGATIVE_FIELD_IDS = frozenset({
    "is_nominee_shareholding",
    "is_employee_shareholding",
    "is_high_tech_manufacturing",
    "new_industry_business_model",
})
INFERENCE_MARKERS = ("未明确", "未见", "未提及", "未发现", "无独立", "无相关", "未披露")


def normalize_agent_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply document-only business rules before agent results are persisted."""
    normalized: list[dict[str, Any]] = []
    for original in fields:
        field = dict(original)
        field_id = field.get("field_id")
        if field_id in EXTERNAL_DATA_FIELD_IDS:
            field.update(status="missing", value=None, evidence=[])
            warnings = list(field.get("warnings") or [])
            warnings.append("Skipped: requires external or group master data.")
            field["warnings"] = warnings
        elif field_id == "financial_forecast_table":
            field["value"] = _normalize_forecast_table(field.get("value"))
            field["status"] = "filled" if field["value"] else "missing"
        elif field_id in DISCLOSURE_REQUIRED_NEGATIVE_FIELD_IDS:
            field = _normalize_unsupported_negative(field)
        elif field_id == "has_industry_market_analysis":
            field = _normalize_market_analysis(field)
        elif field_id == "managing_unit":
            field = _normalize_managing_unit(field)
        elif field_id in MAX_LENGTH_BY_FIELD_ID and isinstance(field.get("value"), str):
            limit = MAX_LENGTH_BY_FIELD_ID[field_id]
            if len(field["value"]) > limit:
                field["value"] = field["value"][:limit]
                warnings = list(field.get("warnings") or [])
                warnings.append(f"Truncated to the field limit of {limit} characters.")
                field["warnings"] = warnings
        normalized.append(field)
    return normalized


def _normalize_forecast_table(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("类别") or "").strip()
        row = {"类别": FORECAST_CATEGORY_ALIASES.get(category, category)}
        row.update({column: raw.get(column, "") for column in FORECAST_COLUMNS})
        rows.append(row)
    return rows


def _normalize_unsupported_negative(field: dict[str, Any]) -> dict[str, Any]:
    if field.get("value") != "否":
        return field
    support_text = _support_text(field)
    if not any(marker in support_text for marker in INFERENCE_MARKERS):
        return field
    normalized = dict(field)
    normalized.update(status="filled", value="未明确", evidence=[])
    warnings = list(normalized.get("warnings") or [])
    warnings.append("Changed to 未明确: absence of disclosure is not an explicit negative.")
    normalized["warnings"] = warnings
    return normalized


def _normalize_market_analysis(field: dict[str, Any]) -> dict[str, Any]:
    if field.get("value") != "否" or "无独立" not in _support_text(field):
        return field
    normalized = dict(field)
    normalized.update(status="filled", value="未明确", evidence=[])
    warnings = list(normalized.get("warnings") or [])
    warnings.append("An absent standalone chapter does not prove that market analysis is absent.")
    normalized["warnings"] = warnings
    return normalized


def _normalize_managing_unit(field: dict[str, Any]) -> dict[str, Any]:
    value = str(field.get("value") or "")
    if not value:
        return field
    for evidence in field.get("evidence") or []:
        quote = str(evidence.get("quote") or "")
        if "隶属于" not in quote or value not in quote:
            continue
        tail = quote.split(value, 1)[1]
        match = re.match(r"([\u4e00-\u9fff]{2,}(?:研究院|研究所|管理局|事业部|中心|有限公司))", tail)
        if not match:
            continue
        normalized = dict(field)
        normalized["value"] = match.group(1)
        warnings = list(normalized.get("warnings") or [])
        warnings.append("Selected the immediate unit from the disclosed affiliation chain.")
        normalized["warnings"] = warnings
        return normalized
    return field


def _support_text(field: dict[str, Any]) -> str:
    quotes = [str(item.get("quote") or "") for item in field.get("evidence") or []]
    warnings = [str(item) for item in field.get("warnings") or []]
    return " ".join([*quotes, *warnings])
