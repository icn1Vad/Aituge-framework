from __future__ import annotations

import pytest

from task_manager.payload_schemas import validate_input_payload, validate_output_payload
from task_manager.registry import get_task_definition


def lookup_input(**overrides):
    payload = {
        "template_id": "female_recruitment_h2",
        "event_name": "女兵下半年征兵",
        "cycle_year": 2026,
        "lookup_number": 0,
        "lookup_type": "bootstrap_search",
        "official_domains": ["gfbzb.gov.cn", "gov.cn"],
        "preferred_official_domains": ["gfbzb.gov.cn"],
        "keywords": ["女兵征兵", "下半年征兵"],
        "phases": [
            {
                "phase_key": "registration_start",
                "phase_name": "报名开始",
                "evidence_keywords": ["报名", "开始"],
                "known_source_url": "",
                "forecast_date": None,
            },
            {
                "phase_key": "registration_deadline",
                "phase_name": "报名截止",
                "evidence_keywords": ["报名", "截止"],
                "known_source_url": "",
                "forecast_date": None,
            },
        ],
    }
    payload.update(overrides)
    return payload


def test_calendar_lookup_task_uses_new_architecture_web_search():
    definition = get_task_definition("media.calendar.official_date.lookup")
    assert definition.handler == "scheduler"
    assert definition.default_skill_package == "media-calendar-official-date-lookup-package"
    assert definition.default_primary_skill == "media-calendar-official-date-lookup"
    assert definition.default_tools == ["web_search"]
    assert definition.input_schema_name == "industry_calendar_official_date_lookup_input"
    assert definition.output_schema_name == "industry_calendar_official_date_lookup_output"


def test_calendar_lookup_input_requires_official_domains():
    validated = validate_input_payload("industry_calendar_official_date_lookup_input", lookup_input())
    assert validated["official_domains"] == ["gfbzb.gov.cn", "gov.cn"]
    assert [item["phase_key"] for item in validated["phases"]] == [
        "registration_start",
        "registration_deadline",
    ]
    with pytest.raises(ValueError, match="official_domains"):
        validate_input_payload(
            "industry_calendar_official_date_lookup_input",
            lookup_input(official_domains=[]),
        )


def test_confirmed_lookup_output_requires_complete_evidence():
    phase = {
        "phase_key": "registration_start",
        "status": "confirmed",
        "found": True,
        "official": True,
        "start_date": "2026-07-01",
        "end_date": "2026-07-01",
        "deadline_at": None,
        "source_url": "https://www.gfbzb.gov.cn/nvbing/",
        "source_title": "义务兵征集（女兵）",
        "source_published_at": None,
        "extraction_method": "official_search",
        "evidence_summary": "官方页面明确列出下半年报名窗口。",
        "evidence_excerpt": "下半年应征报名：2026年7月1日至2026年8月10日24时。",
        "warnings": [],
    }
    valid = {"status": "completed", "phase_results": [phase], "source_urls": [phase["source_url"]], "warnings": []}
    valid_result, error = validate_output_payload(
        "industry_calendar_official_date_lookup_output",
        valid,
    )
    assert valid_result is True and error is None
    valid_result, error = validate_output_payload(
        "industry_calendar_official_date_lookup_output",
        {**valid, "phase_results": [{**phase, "source_url": ""}]},
    )
    assert valid_result is False
    assert error and error["schema_name"] == "industry_calendar_official_date_lookup_output"


def test_not_found_lookup_does_not_invent_dates():
    valid_result, error = validate_output_payload(
        "industry_calendar_official_date_lookup_output",
        {
            "status": "not_found",
            "phase_results": [{
                "phase_key": "registration_start",
                "status": "awaiting_official",
                "found": False,
                "official": False,
                "start_date": None,
                "end_date": None,
                "deadline_at": None,
                "source_url": "",
                "source_title": "",
                "source_published_at": None,
                "extraction_method": "official_search",
                "evidence_summary": "未找到白名单官方来源。",
                "evidence_excerpt": "",
                "warnings": ["保持 awaiting_official"],
            }],
            "source_urls": [],
            "warnings": ["保持 awaiting_official"],
        },
    )
    assert valid_result is True and error is None
