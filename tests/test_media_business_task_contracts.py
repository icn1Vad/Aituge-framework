from __future__ import annotations

from skill.package_service import DEFAULT_SKILL_PACKAGES
from task_manager.payload_schemas import validate_input_payload, validate_output_payload
from task_manager.registry import get_task_definition


def test_media_business_tasks_are_registered_with_existing_handlers():
    expected = {
        "media.calendar.official_date.lookup": ("scheduler", "web_search"),
        "media.topic.history_viral.variants.generate": ("batch_item_scheduler", None),
        "media.topic.industry_calendar.copy.generate": ("batch_item_scheduler", None),
        "analytics.douyin.content_analysis.batch": ("batch_item_scheduler", None),
    }
    for task_type, (handler, tool) in expected.items():
        definition = get_task_definition(task_type)
        assert definition.handler == handler
        assert (tool in definition.default_tools) if tool else definition.default_tools == []


def test_media_business_skill_packages_are_seeded():
    package_names = {item["package_name"] for item in DEFAULT_SKILL_PACKAGES}
    assert {
        "media-calendar-official-date-lookup-package",
        "media-history-viral-topic-variants-package",
        "media-industry-calendar-topic-copy-package",
        "douyin-content-analysis-package",
    } <= package_names


def test_calendar_lookup_contract_requires_official_evidence():
    validated = validate_input_payload(
        "industry_calendar_official_date_lookup_input",
        {
            "template_id": "recruitment",
            "event_name": "Recruitment",
            "cycle_year": 2026,
            "lookup_number": 0,
            "lookup_type": "bootstrap_search",
            "official_domains": ["gov.cn"],
            "phases": [{
                "phase_key": "registration",
                "phase_name": "Registration",
                "evidence_keywords": ["registration"],
            }],
        },
    )
    assert validated["official_domains"] == ["gov.cn"]
    valid, _ = validate_output_payload(
        "industry_calendar_official_date_lookup_output",
        {
            "status": "completed",
            "phase_results": [{
                "phase_key": "registration",
                "status": "confirmed",
                "found": True,
                "official": True,
                "start_date": "2026-07-01",
                "end_date": "2026-07-01",
                "source_url": "https://www.gov.cn/example",
                "source_title": "Official notice",
                "extraction_method": "official_search",
                "evidence_excerpt": "Registration starts on 2026-07-01.",
            }],
            "source_urls": ["https://www.gov.cn/example"],
        },
    )
    assert valid is True


def test_batch_business_input_contracts_validate_real_payload_shapes():
    history = validate_input_payload(
        "history_viral_topic_variants_input",
        {
            "account_id": "acct-1",
            "business_date": "2026-07-14",
            "content_snapshot": "snapshot-12345678",
            "items": [{
                "id": "video-1",
                "original_content_id": "video-1",
                "original_title": "A reusable topic",
                "original_publish_time": "2026-06-20T08:00:00+08:00",
                "account_percentile_score": 0.9,
                "metric_percentiles": {"play_count": 0.95},
            }],
        },
    )
    assert history["items"][0]["id"] == "video-1"

    analysis = validate_input_payload(
        "douyin_content_analysis_batch_input",
        {
            "account_id": "acct-1",
            "period": {"days": 30},
            "items": [{
                "id": "top:video-1",
                "content_id": "video-1",
                "rank_type": "best",
                "rank": 1,
                "evidence": {"play_count": 100},
            }],
        },
    )
    assert analysis["items"][0]["content_id"] == "video-1"
