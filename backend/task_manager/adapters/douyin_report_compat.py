from __future__ import annotations

from typing import Any


def add_legacy_monthly_report(structured: Any, input_payload: dict[str, Any]) -> Any:
    if not isinstance(structured, dict):
        return structured
    if structured.get("legacy_monthly_report"):
        return structured
    compat = build_legacy_monthly_report(structured, input_payload)
    return {**structured, "legacy_monthly_report": compat}


def build_legacy_monthly_report(structured: dict[str, Any], input_payload: dict[str, Any]) -> dict[str, Any]:
    sections = structured.get("sections") if isinstance(structured.get("sections"), dict) else {}
    metrics = structured.get("key_metrics") if isinstance(structured.get("key_metrics"), dict) else {}
    report_context = input_payload.get("report_context") if isinstance(input_payload.get("report_context"), dict) else {}
    context_metrics = (
        report_context.get("metrics_summary") if isinstance(report_context.get("metrics_summary"), dict) else {}
    )
    payload_metrics = input_payload.get("metrics_summary") if isinstance(input_payload.get("metrics_summary"), dict) else {}
    content_items = input_payload.get("content_items") if isinstance(input_payload.get("content_items"), list) else []
    missing_fields = _string_list(structured.get("missing_fields") or input_payload.get("missing_fields"))
    warnings = _string_list(structured.get("warnings") or input_payload.get("warnings"))

    overall = _section(sections.get("overall_performance"))
    growth = _section(sections.get("growth_and_traffic"))
    content = _section(sections.get("content_performance"))
    interaction = _section(sections.get("interaction_and_comments"))
    rhythm = _section(sections.get("publishing_rhythm"))
    risks = _section(sections.get("risks_and_limitations"))

    key_metrics = {**context_metrics, **payload_metrics, **metrics}
    top_items = _analysis_items(structured.get("top_content_analysis"))
    low_items = _analysis_items(structured.get("low_content_analysis"))

    return {
        "status": structured.get("status") or "partial_data",
        "source_status": structured.get("status") or "partial_data",
        "warnings": warnings,
        "missing_fields": missing_fields,
        "sections": {
            "conclusion": {
                "summary": structured.get("executive_summary") or overall["summary"],
                "overall_summary": structured.get("executive_summary") or overall["summary"],
                "key_metrics": key_metrics,
                "key_conclusions": overall["findings"] or _string_list(structured.get("next_month_actions"))[:4],
                "top_content_analysis": _join_blocks(
                    [
                        _items_to_sentence("高表现内容", top_items),
                        content["summary"],
                    ]
                ),
                "content_quality_assessment": content["summary"],
                "publishing_rhythm_analysis": rhythm["summary"],
                "comparison_status": {
                    "previous_month": "暂未获取到上期对比数据，当前报告不写环比。",
                    "benchmark": "暂未获取到行业或同类账号 benchmark，当前报告不写同类对标。",
                },
                "highlights": overall["evidence"][:4] or _items_to_bullets(top_items),
                "risks": risks["findings"][:4] or risks["limitations"][:4],
                "data_limitations": _string_list(structured.get("data_limitations")) or risks["limitations"],
                "evidence": overall["evidence"][:6],
                "next_step_suggestions": _string_list(structured.get("next_month_actions")) or risks["next_actions"],
            },
            "audience_and_traffic": {
                "summary": growth["summary"],
                "audience_summary": _missing_text(
                    missing_fields,
                    ["age_distribution", "gender_distribution", "region_distribution"],
                    "暂未获取到年龄、性别、地域等受众画像，不能判断具体人群结构。",
                ),
                "available_metrics": {
                    key: key_metrics.get(key)
                    for key in ("fans_count", "new_fans_count", "profile_visit_count", "play_count", "interaction_rate")
                    if key in key_metrics
                },
                "missing_profile_fields": [
                    field
                    for field in missing_fields
                    if field in {"age_distribution", "gender_distribution", "region_distribution", "traffic_source"}
                ],
                "fan_growth_analysis": growth["summary"],
                "traffic_potential_analysis": _join_blocks(growth["findings"][:3]),
                "audience_profile_status": "暂未获取到完整受众画像。" if missing_fields else "",
                "publishing_time_status": rhythm["summary"],
                "data_limitations": growth["limitations"] or risks["limitations"],
                "next_step_suggestions": growth["next_actions"] or rhythm["next_actions"],
            },
            "interaction_and_comments": {
                "summary": interaction["summary"],
                "comment_count_analysis": interaction["summary"]
                or _metric_sentence(key_metrics, "comment_count", "评论数")
                or "暂未获取到评论数或评论正文，当前不能判断评论规模、评论情绪和用户高频问题。",
                "top_comment_contents": [],
                "missing_comment_fields": [
                    field
                    for field in missing_fields
                    if field in {"comment_text", "comment_keywords", "sentiment", "typical_comments", "user_questions"}
                ],
                "data_limitations": interaction["limitations"] or ["暂未获取到评论正文，不能分析具体用户问题和情绪。"],
            },
            "content_and_script_clues": {
                "summary": content["summary"],
                "content_performance": _content_performance(content_items),
                "retention_metrics": _retention_metrics(content_items),
                "script_text_status": "暂未获取到完整脚本文本，不能评价脚本质量，只能根据播放、互动和留存线索判断内容表现。",
                "data_limitations": content["limitations"] or risks["limitations"],
                "suggestions_based_on_data": content["next_actions"] or _items_to_actions(top_items, low_items),
            },
        },
    }


def _section(value: Any) -> dict[str, list[str] | str]:
    data = value if isinstance(value, dict) else {}
    return {
        "summary": str(data.get("summary") or "").strip(),
        "findings": _string_list(data.get("findings")),
        "evidence": _string_list(data.get("evidence")),
        "limitations": _string_list(data.get("limitations")),
        "next_actions": _string_list(data.get("next_actions")),
    }


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _analysis_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _items_to_sentence(title: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    parts = []
    for item in items[:3]:
        name = item.get("title") or item.get("id") or "未命名内容"
        reason = item.get("reason") or item.get("observation") or ""
        play_count = item.get("play_count")
        metric = f"播放 {play_count}" if play_count is not None else ""
        parts.append("，".join(part for part in [str(name), metric, str(reason)] if part))
    return f"{title}：" + "；".join(parts)


def _items_to_bullets(items: list[dict[str, Any]]) -> list[str]:
    return [
        "，".join(str(part) for part in [item.get("title") or item.get("id"), item.get("reason")] if part)
        for item in items[:4]
    ]


def _items_to_actions(top_items: list[dict[str, Any]], low_items: list[dict[str, Any]]) -> list[str]:
    actions = []
    for item in top_items[:2]:
        action = item.get("recommended_action")
        if action:
            actions.append(str(action))
    for item in low_items[:2]:
        action = item.get("recommended_action")
        if action:
            actions.append(str(action))
    return actions


def _content_performance(items: list[Any]) -> list[dict[str, Any]]:
    rows = [item for item in items if isinstance(item, dict)]
    keys = ["id", "title", "play_count", "like_count", "comment_count", "share_count", "collect_count"]
    return [{key: item.get(key) for key in keys if key in item} for item in rows[:12]]


def _retention_metrics(items: list[Any]) -> dict[str, Any]:
    rows = [item for item in items if isinstance(item, dict)]
    values: dict[str, Any] = {}
    for key in ("avg_view_second", "completion_rate", "completion_rate_5s", "bounce_rate_2s"):
        present = [item.get(key) for item in rows if item.get(key) is not None]
        if present:
            values[f"{key}_available_count"] = len(present)
    return values


def _join_blocks(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(item for item in _string_list(value))
    return str(value or "").strip()


def _missing_text(missing_fields: list[str], fields: list[str], fallback: str) -> str:
    return fallback if any(field in missing_fields for field in fields) else ""


def _metric_sentence(metrics: dict[str, Any], key: str, label: str) -> str:
    if key not in metrics:
        return ""
    return f"{label}：{metrics.get(key)}。"
