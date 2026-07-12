import asyncio
import json

import httpx
import pytest

from backend.local_code_chat_app import create_app
from db.db_context import init_db, reset_engine_for_test
from skill import SkillManager
import task_manager.handlers.batch_item_scheduler as batch_handler
from task_manager.payload_schemas import validate_input_payload, validate_output_payload
from task_manager.registry import get_task_definition


def history_input(**overrides):
    payload = {
        "account_id": "acct-1",
        "business_date": "2026-07-12",
        "timezone": "Asia/Shanghai",
        "content_snapshot": "snapshot-12345678",
        "variants_per_source": 3,
        "max_concurrency": 4,
        "failure_policy": "continue",
        "retry_per_item": 0,
        "items": [
            {
                "id": "video-1",
                "original_content_id": "video-1",
                "original_title": "退役前如何规划技能证书",
                "original_publish_time": "2026-06-20T08:00:00+08:00",
                "source_url": "https://example.test/video-1",
                "account_percentile_score": 0.86,
                "metric_percentiles": {
                    "play_count": 0.91,
                    "like_count": 0,
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


def calendar_item(**overrides):
    payload = {
        "id": "army-day-2026",
        "event_id": "army-day-2026",
        "event_name": "建军节",
        "cycle_year": 2026,
        "phase_key": "main",
        "trigger_phase": "early_warmup",
        "event_status": "fixed",
        "event_date": "2026-08-01",
        "days_until_event": 10,
        "source_url": "",
        "official_published_at": None,
        "official_source_title": "",
        "valid_from": "2026-07-22",
        "valid_until": "2026-08-01",
        "keywords": ["建军节", "军旅成长"],
    }
    payload.update(overrides)
    return payload


def calendar_input(*, items=None, **overrides):
    payload = {
        "business_date": "2026-07-22",
        "timezone": "Asia/Shanghai",
        "calendar_revision": 7,
        "config_version": "2026.1",
        "max_concurrency": 4,
        "failure_policy": "continue",
        "retry_per_item": 0,
        "items": items or [calendar_item()],
    }
    payload.update(overrides)
    return payload


def test_recommendation_task_definitions_use_batch_items_and_business_skills():
    history = get_task_definition("media.topic.history_viral.variants.generate")
    assert history.handler == "batch_item_scheduler"
    assert history.default_skill_package == "media-history-viral-topic-variants-package"
    assert history.default_primary_skill == "media-history-viral-topic-variants"
    assert history.default_tools == []
    assert history.default_datasets == []
    assert history.input_schema_name == "history_viral_topic_variants_input"
    assert history.output_schema_name == "batch_task_output"
    assert history.item_output_schema_name == "history_viral_topic_variant_item_output"

    calendar = get_task_definition("media.topic.industry_calendar.copy.generate")
    assert calendar.handler == "batch_item_scheduler"
    assert calendar.default_skill_package == "media-industry-calendar-topic-copy-package"
    assert calendar.default_primary_skill == "media-industry-calendar-topic-copy"
    assert calendar.default_tools == []
    assert calendar.default_datasets == []
    assert calendar.input_schema_name == "industry_calendar_topic_copy_input"
    assert calendar.output_schema_name == "batch_task_output"
    assert calendar.item_output_schema_name == "industry_calendar_topic_copy_item_output"


def test_history_schema_preserves_real_zero_and_omits_missing_metrics():
    validated = validate_input_payload("history_viral_topic_variants_input", history_input())
    percentiles = validated["items"][0]["metric_percentiles"]
    assert percentiles == {"play_count": 0.91, "like_count": 0.0}
    assert validated["items"][0]["id"] == "video-1"
    assert validated["failure_policy"] == "continue"


def test_history_schema_rejects_negative_metrics_and_duplicate_sources():
    negative = history_input()
    negative["items"][0]["metric_percentiles"] = {"play_count": -0.01}
    with pytest.raises(ValueError, match="history_viral_topic_variants_input"):
        validate_input_payload("history_viral_topic_variants_input", negative)

    duplicate = history_input()
    duplicate["items"] = [duplicate["items"][0], dict(duplicate["items"][0])]
    with pytest.raises(ValueError, match="unique"):
        validate_input_payload("history_viral_topic_variants_input", duplicate)

    missing_title = history_input()
    missing_title["items"][0]["original_title"] = "   "
    with pytest.raises(ValueError, match="history_viral_topic_variants_input"):
        validate_input_payload("history_viral_topic_variants_input", missing_title)


def test_history_item_output_enforces_three_distinct_topic_angles():
    valid = {
        "source_item_id": "video-1",
        "status": "generated",
        "variants": [
            {
                "variant_index": 1,
                "generated_topic": "退役前半年，证书规划先做哪三步",
                "variant_angle": "行动清单",
                "recommendation_reason": "把历史主题转成可执行的准备顺序。",
                "relation_to_source": "保留证书规划主题，改为退役前半年行动清单。",
            },
            {
                "variant_index": 2,
                "generated_topic": "证书越多越好吗？退役技能规划的三个误区",
                "variant_angle": "误区拆解",
                "recommendation_reason": "通过常见误区形成新的决策视角。",
                "relation_to_source": "保留技能证书主题，改为风险和误区视角。",
            },
        ],
        "warnings": [],
    }
    assert validate_output_payload("history_viral_topic_variant_item_output", valid)[0]

    duplicate_angle = json.loads(json.dumps(valid, ensure_ascii=False))
    duplicate_angle["variants"][1]["variant_angle"] = "行动清单"
    with pytest.raises(ValueError, match="history_viral_topic_variant_item_output"):
        validate_output_payload(
            "history_viral_topic_variant_item_output",
            duplicate_angle,
        )

    too_many = json.loads(json.dumps(valid, ensure_ascii=False))
    too_many["variants"] = [too_many["variants"][0]] * 4
    with pytest.raises(ValueError, match="history_viral_topic_variant_item_output"):
        validate_output_payload("history_viral_topic_variant_item_output", too_many)


@pytest.mark.parametrize("status", ["awaiting_official", "candidate", "conflict"])
def test_calendar_schema_rejects_non_formal_statuses(status):
    with pytest.raises(ValueError, match="industry_calendar_topic_copy_input"):
        validate_input_payload(
            "industry_calendar_topic_copy_input",
            calendar_input(items=[calendar_item(event_status=status)]),
        )


def test_calendar_schema_accepts_fixed_and_confirmed_date_range():
    fixed = validate_input_payload("industry_calendar_topic_copy_input", calendar_input())
    assert fixed["items"][0]["event_status"] == "fixed"
    assert fixed["items"][0]["event_date"] == "2026-08-01"

    confirmed_item = calendar_item(
        id="annual-registration-2026",
        event_id="annual-registration-2026",
        event_name="年度报名窗口",
        trigger_phase="mid_warmup",
        event_status="confirmed",
        event_date=None,
        date_range={"start": "2026-07-26", "end": "2026-07-29"},
        days_until_event=4,
        source_url="https://gov.example/notice",
        official_published_at="2026-07-10T09:00:00+08:00",
        official_source_title="年度报名通知",
    )
    confirmed = validate_input_payload(
        "industry_calendar_topic_copy_input",
        calendar_input(items=[confirmed_item]),
    )
    assert confirmed["items"][0]["date_range"]["end"] == "2026-07-29"

    with pytest.raises(ValueError, match="source_url"):
        validate_input_payload(
            "industry_calendar_topic_copy_input",
            calendar_input(items=[{**confirmed_item, "source_url": ""}]),
        )


def test_calendar_schema_rejects_ambiguous_dates_and_wrong_day_offset():
    both = calendar_item(date_range={"start": "2026-08-01", "end": "2026-08-01"})
    with pytest.raises(ValueError, match="Exactly one"):
        validate_input_payload(
            "industry_calendar_topic_copy_input",
            calendar_input(items=[both]),
        )

    with pytest.raises(ValueError, match="days_until_event"):
        validate_input_payload(
            "industry_calendar_topic_copy_input",
            calendar_input(items=[calendar_item(days_until_event=9)]),
        )


def test_calendar_item_output_requires_complete_or_empty_copy():
    valid = {
        "source_item_id": "army-day-2026",
        "status": "generated",
        "generated_topic": "建军节前十天，先读懂军人职业成长的三条主线",
        "variant_angle": "前期科普",
        "recommendation_reason": "适合节点前十天进行背景认知导入。",
        "warnings": [],
    }
    assert validate_output_payload("industry_calendar_topic_copy_item_output", valid)[0]
    invalid = {**valid, "recommendation_reason": ""}
    with pytest.raises(ValueError, match="industry_calendar_topic_copy_item_output"):
        validate_output_payload("industry_calendar_topic_copy_item_output", invalid)
    insufficient = {
        "source_item_id": "army-day-2026",
        "status": "insufficient_context",
        "generated_topic": "",
        "variant_angle": "",
        "recommendation_reason": "",
        "warnings": ["No safe angle from the supplied facts."],
    }
    assert validate_output_payload("industry_calendar_topic_copy_item_output", insufficient)[0]


def test_recommendation_skill_packages_load_without_tools(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recommendation-skills.db'}")
        reset_engine_for_test()
        await init_db()

        history = await SkillManager().create_context("media-history-viral-topic-variants-package")
        calendar = await SkillManager().create_context("media-industry-calendar-topic-copy-package")

        assert history.tools == []
        assert "# Media History Viral Topic Variants" in history.task_prompt
        assert history.skills["active_package"]["primary"]["name"] == (
            "media-history-viral-topic-variants"
        )
        assert calendar.tools == []
        assert "# Media Industry Calendar Topic Copy" in calendar.task_prompt
        assert calendar.skills["active_package"]["primary"]["name"] == (
            "media-industry-calendar-topic-copy"
        )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_history_batch_item_failure_does_not_block_other_items(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recommendation-batch.db'}")
        reset_engine_for_test()
        await init_db()

        async def fake_run_scheduler_for_item(*, item, **_kwargs):
            if item.item_key == "video-fail":
                raise RuntimeError("synthetic item failure")
            output = {
                "source_item_id": item.item_key,
                "status": "generated",
                "variants": [
                    {
                        "variant_index": 1,
                        "generated_topic": "退役前半年，技能证书规划先做哪三步",
                        "variant_angle": "行动清单",
                        "recommendation_reason": "把历史主题转成可执行的准备顺序。",
                        "relation_to_source": "保留证书规划主题并改为行动清单。",
                    }
                ],
                "warnings": [],
            }
            return json.dumps(output, ensure_ascii=False), {"total_tokens": 12}

        monkeypatch.setattr(batch_handler, "_run_scheduler_for_item", fake_run_scheduler_for_item)
        payload = history_input()
        failed_item = {
            **payload["items"][0],
            "id": "video-fail",
            "original_content_id": "video-fail",
            "original_title": "A source that fails in the synthetic agent",
        }
        payload["items"] = [payload["items"][0], failed_item]

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/task-manager/run",
                headers={"X-User-Id": "recommendation-test-user"},
                json={
                    "task_type": "media.topic.history_viral.variants.generate",
                    "title": "History variants batch",
                    "stream": False,
                    "input_payload": payload,
                },
            )

        assert response.status_code == 200
        body = response.json()
        task = body["task"]
        assert task["status"] == "succeeded"
        structured = task["result_payload_json"]["structured"]
        assert structured["summary"] == {
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "skipped": 0,
        }
        by_key = {item["item_key"]: item for item in structured["items"]}
        assert by_key["video-1"]["status"] == "succeeded"
        assert by_key["video-1"]["result"]["result"]["status"] == "generated"
        assert by_key["video-fail"]["status"] == "failed"
        assert by_key["video-fail"]["error"]["message"] == "synthetic item failure"
        assert any(event["event_type"] == "item_failed" for event in body["events"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_history_batch_schema_failure_marks_only_that_item_failed(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recommendation-schema-failure.db'}")
        reset_engine_for_test()
        await init_db()

        async def fake_run_scheduler_for_item(*, item, **_kwargs):
            output = {
                "source_item_id": item.item_key,
                "status": "generated",
                "variants": [
                    {
                        "variant_index": 1,
                        "generated_topic": "合法选题" if item.item_key == "video-1" else "含非法字段的选题",
                        "variant_angle": "行动清单",
                        "recommendation_reason": "结构化原因。",
                        "relation_to_source": "保留主题并改变角度。",
                        **({} if item.item_key == "video-1" else {"forged_source_url": "https://untrusted.test"}),
                    }
                ],
                "warnings": [],
            }
            return json.dumps(output, ensure_ascii=False), {"total_tokens": 12}

        monkeypatch.setattr(batch_handler, "_run_scheduler_for_item", fake_run_scheduler_for_item)
        payload = history_input()
        invalid_item = {
            **payload["items"][0],
            "id": "video-invalid-schema",
            "original_content_id": "video-invalid-schema",
            "original_title": "用于验证 schema 失败状态",
        }
        payload["items"] = [payload["items"][0], invalid_item]

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/task-manager/run",
                headers={"X-User-Id": "recommendation-schema-test-user"},
                json={
                    "task_type": "media.topic.history_viral.variants.generate",
                    "title": "Strict history output validation",
                    "stream": False,
                    "input_payload": payload,
                },
            )

        assert response.status_code == 200
        structured = response.json()["task"]["result_payload_json"]["structured"]
        assert structured["summary"] == {"total": 2, "succeeded": 1, "failed": 1, "skipped": 0}
        by_key = {item["item_key"]: item for item in structured["items"]}
        assert by_key["video-1"]["status"] == "succeeded"
        assert by_key["video-invalid-schema"]["status"] == "failed"
        assert by_key["video-invalid-schema"]["error"]["type"] == "TaskItemOutputValidationError"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
