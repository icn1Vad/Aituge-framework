from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_LEGACY_DOUYIN_API_BASE_URL = "http://127.0.0.1:8010"


async def enrich_douyin_account_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Fill a Douyin account report task from the legacy media API when requested."""
    if _has_report_data(payload) and payload.get("data_source") != "legacy_douyin_api":
        return payload
    data_source = str(payload.get("data_source") or "").strip()
    if data_source and data_source != "legacy_douyin_api":
        return payload
    if not data_source and not _should_auto_fetch(payload):
        return payload

    base_url = str(
        payload.get("legacy_api_base_url")
        or os.getenv("LEGACY_DOUYIN_API_BASE_URL")
        or DEFAULT_LEGACY_DOUYIN_API_BASE_URL
    ).rstrip("/")
    account_id = str(payload.get("account_id") or "").strip()
    content_limit = _bounded_int(payload.get("content_limit"), default=50, minimum=1, maximum=200)

    try:
        overview, contents = await _fetch_legacy_douyin_data(
            base_url=base_url,
            account_id=account_id,
            content_limit=content_limit,
        )
    except httpx.HTTPError as exc:
        raise ValueError(f"Legacy Douyin API fetch failed: {exc}") from exc
    return build_douyin_account_report_payload(
        original=payload,
        overview=overview,
        contents_response=contents,
        base_url=base_url,
        content_limit=content_limit,
    )


def build_douyin_account_report_payload(
    *,
    original: dict[str, Any],
    overview: dict[str, Any],
    contents_response: dict[str, Any],
    base_url: str,
    content_limit: int,
) -> dict[str, Any]:
    account = overview.get("account") if isinstance(overview.get("account"), dict) else {}
    metrics = overview.get("metrics") if isinstance(overview.get("metrics"), dict) else {}
    raw_contents = contents_response.get("items") if isinstance(contents_response.get("items"), list) else []
    content_items = [_normalize_content_item(item) for item in raw_contents if isinstance(item, dict)]
    content_items = [item for item in content_items if item]
    content_items.sort(key=lambda item: (item.get("play_count") or 0, item.get("like_count") or 0), reverse=True)

    account_id = str(original.get("account_id") or account.get("id") or metrics.get("account_id") or "")
    account_name = str(original.get("account_name") or account.get("account_name") or "")
    metrics_summary = _normalize_metrics(metrics, content_items)
    missing_fields = sorted(set(_default_missing_fields(metrics_summary, content_items)) | set(original.get("missing_fields") or []))
    warnings = list(original.get("warnings") or [])
    warnings.append("report_context was filled from legacy Douyin API.")
    if not content_items:
        warnings.append("Legacy Douyin API returned no content_items; report will rely on account-level metrics only.")

    report_context = {
        "source": "legacy_douyin_api",
        "legacy_api_base_url": base_url,
        "content_limit": content_limit,
        "account": account,
        "metrics": metrics,
        "metrics_summary": metrics_summary,
        "data_coverage": {
            "scope": "all_available_data",
            "account_metrics": "available" if metrics else "missing_data",
            "content_metrics": "available" if content_items else "missing_data",
            "content_count": len(content_items),
            "audience_profile": "missing_data",
            "comment_content": "missing_permission",
        },
        "missing_fields": missing_fields,
        "warnings": warnings,
    }

    return {
        **original,
        "data_source": "legacy_douyin_api",
        "account_id": account_id,
        "account_name": account_name,
        "platform": "douyin",
        "analysis_scope": original.get("analysis_scope") or "all_data",
        "report_depth": original.get("report_depth") or "deep",
        "report_goal": original.get("report_goal")
        or "Generate a complete Douyin account operations analysis report from all available legacy data.",
        "report_context": {**report_context, **(original.get("report_context") or {})},
        "metrics_summary": {**metrics_summary, **(original.get("metrics_summary") or {})},
        "content_items": original.get("content_items") or content_items,
        "top_contents": original.get("top_contents") or content_items[:3],
        "low_contents": original.get("low_contents") or list(reversed(content_items[-3:])),
        "warnings": warnings,
        "missing_fields": missing_fields,
    }


async def _fetch_legacy_douyin_data(
    *,
    base_url: str,
    account_id: str,
    content_limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    params = {"account_id": account_id} if account_id else {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        overview_response = await client.get(f"{base_url}/analytics/douyin/overview", params=params)
        overview_response.raise_for_status()
        content_params: dict[str, Any] = {"limit": content_limit}
        if account_id:
            content_params["account_id"] = account_id
        contents_response = await client.get(f"{base_url}/analytics/douyin/contents", params=content_params)
        contents_response.raise_for_status()
    return overview_response.json(), contents_response.json()


def _normalize_metrics(metrics: dict[str, Any], content_items: list[dict[str, Any]]) -> dict[str, Any]:
    content_totals = {
        key: sum(_to_int(item.get(key)) for item in content_items)
        for key in ("play_count", "like_count", "comment_count", "share_count", "collect_count")
    }
    summary = {
        "fans_count": _to_int(metrics.get("fans_count")),
        "new_fans_count": _to_int(metrics.get("new_fans_count")),
        "profile_visit_count": _to_int(metrics.get("profile_visit_count")),
        "publish_count": len(content_items) or _to_int(metrics.get("publish_count")),
        "play_count": content_totals["play_count"] or _to_int(metrics.get("play_count")),
        "like_count": content_totals["like_count"] or _to_int(metrics.get("like_count")),
        "comment_count": content_totals["comment_count"] or _to_int(metrics.get("comment_count")),
        "share_count": content_totals["share_count"] or _to_int(metrics.get("share_count")),
        "collect_count": content_totals["collect_count"] or _to_int(metrics.get("collect_count")),
        "overview_play_count": _to_int(metrics.get("play_count")),
        "overview_like_count": _to_int(metrics.get("like_count")),
        "overview_comment_count": _to_int(metrics.get("comment_count")),
        "overview_share_count": _to_int(metrics.get("share_count")),
        "overview_collect_count": _to_int(metrics.get("collect_count")),
        "snapshot_date": metrics.get("date"),
    }
    total_play = summary.get("play_count") or 0
    interactions = sum(summary.get(key) or 0 for key in ("like_count", "comment_count", "share_count", "collect_count"))
    summary["interaction_rate"] = round(interactions / total_play, 6) if total_play else 0.0
    return summary


def _normalize_content_item(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    derived = raw.get("derived") if isinstance(raw.get("derived"), dict) else {}
    return {
        "id": str(item.get("content_id") or item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or ""),
        "publish_time": item.get("publish_time"),
        "play_count": _to_int(item.get("play_count")),
        "like_count": _to_int(item.get("like_count")),
        "comment_count": _to_int(item.get("comment_count")),
        "share_count": _to_int(item.get("share_count")),
        "collect_count": _to_int(item.get("collect_count")),
        "avg_view_second": _to_float(derived.get("avg_view_second")),
        "completion_rate": _to_float(derived.get("completion_rate")),
        "completion_rate_5s": _to_float(derived.get("completion_rate_5s")),
        "bounce_rate_2s": _to_float(derived.get("bounce_rate_2s")),
        "homepage_visit_count": _to_int(derived.get("homepage_visit_count")),
    }


def _default_missing_fields(metrics_summary: dict[str, Any], content_items: list[dict[str, Any]]) -> list[str]:
    missing = [
        "age_distribution",
        "gender_distribution",
        "region_distribution",
        "traffic_source",
        "comment_text",
        "comment_keywords",
        "sentiment",
        "full_script_text",
        "benchmark_data",
        "previous_period_data",
    ]
    if not any(item.get("completion_rate") is not None for item in content_items):
        missing.extend(["completion_rate", "completion_rate_5s", "bounce_rate_2s"])
    if not metrics_summary.get("collect_count"):
        missing.append("collect_count")
    return missing


def _has_report_data(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("report_context")
        or payload.get("metrics_summary")
        or payload.get("content_items")
        or payload.get("top_contents")
    )


def _should_auto_fetch(payload: dict[str, Any]) -> bool:
    return bool(payload.get("account_id") or payload.get("legacy_api_base_url"))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _to_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
