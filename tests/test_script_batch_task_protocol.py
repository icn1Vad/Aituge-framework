import asyncio
import json

import httpx
import pytest

from backend.local_code_chat_app import create_app
from db.db_context import init_db, reset_engine_for_test
import task_manager.handlers.batch_item_scheduler as batch_handler
from task_manager.payload_schemas import (
    TaskItemOutputValidationError,
    is_fail_hard_item_output_schema,
    validate_input_payload,
    validate_output_payload,
)
from task_manager.registry import get_task_definition


def daily_item(suffix: str = "1") -> dict:
    return {
        "id": f"run-item-{suffix}",
        "topic_card_id": f"topic-card-{suffix}",
        "candidate_id": f"candidate-{suffix}",
        "topic": f"Daily topic {suffix}",
        "source_type": "daily_channel",
        "recommendation_reason": "Current daily-channel evidence supports this topic.",
        "source_brief": "Use only the supplied evidence and keep claims conservative.",
        "source_refs": [
            {
                "source_type": "daily_channel",
                "source_id": f"daily-source-{suffix}",
                "title": f"Daily source {suffix}",
            }
        ],
        "materials": [],
        "comments": [],
    }


def batch_input(*, items: list[dict] | None = None, **overrides) -> dict:
    payload = {
        "run_id": "today-run-2026-07-12",
        "business_date": "2026-07-12",
        "platform": "douyin",
        "duration_seconds": 60,
        "failure_policy": "continue",
        "retry_per_item": 0,
        "items": items or [daily_item()],
    }
    payload.update(overrides)
    return payload


def valid_script_output(topic: str = "Daily topic") -> dict:
    return {
        "final_script": {
            "topic_name": topic,
            "hook_3s": "Start with one concrete question.",
            "voiceover": "This is a complete, speakable script body for the test fixture.",
        },
        "readable_script": "This is a complete, speakable script body for the test fixture.",
        "hermes_agent_result": {
            "status": "ok",
            "editor_summary": "Structured test output.",
        },
    }


def test_script_batch_definition_reuses_existing_script_package():
    definition = get_task_definition("media.script.batch_generate")

    assert definition.handler == "batch_item_scheduler"
    assert definition.default_skill_package == "media-script-generate-package"
    assert definition.default_primary_skill == "media-script-generator"
    assert definition.default_candidate_skills == ["media-script-selector"]
    assert definition.default_tools == ["rag_retrieval"]
    assert definition.default_datasets == ["local_rag"]
    assert definition.input_schema_name == "media_script_batch_generate_input"
    assert definition.output_schema_name == "batch_task_output"
    assert definition.item_output_schema_name == "media_script_batch_item_output"
    assert is_fail_hard_item_output_schema(definition.item_output_schema_name)
    assert not is_fail_hard_item_output_schema("table_audit_item_output")


def test_script_batch_input_is_strict_and_daily_does_not_require_mission_fields():
    validated = validate_input_payload("media_script_batch_generate_input", batch_input())

    assert validated["items"][0]["source_type"] == "daily_channel"
    assert validated["timezone"] == "Asia/Shanghai"
    assert "source_task_id" not in validated["items"][0]
    assert "source_mission_id" not in validated["items"][0]
    assert "max_concurrency" not in validated

    with pytest.raises(ValueError, match="media_script_batch_generate_input"):
        validate_input_payload(
            "media_script_batch_generate_input",
            batch_input(max_concurrency=4),
        )


def test_script_batch_input_requires_unique_items_and_source_specific_evidence():
    duplicate = daily_item()
    with pytest.raises(ValueError, match="unique"):
        validate_input_payload(
            "media_script_batch_generate_input",
            batch_input(items=[duplicate, dict(duplicate)]),
        )

    history = {
        **daily_item("history"),
        "source_type": "history_viral",
        "source_refs": [
            {
                "source_type": "history_viral",
                "source_id": "history-source-1",
            }
        ],
    }
    with pytest.raises(ValueError, match="original_content_id"):
        validate_input_payload(
            "media_script_batch_generate_input",
            batch_input(items=[history]),
        )

    calendar = {
        **daily_item("calendar"),
        "source_type": "industry_calendar",
        "event_name": "Confirmed event",
        "source_refs": [
            {
                "source_type": "industry_calendar",
                "source_id": "calendar-source-1",
            }
        ],
    }
    with pytest.raises(ValueError, match="event_date"):
        validate_input_payload(
            "media_script_batch_generate_input",
            batch_input(items=[calendar]),
        )


def test_script_batch_output_requires_real_script_text_and_success_state():
    assert validate_output_payload(
        "media_script_batch_item_output",
        valid_script_output(),
    )[0]

    empty_body = valid_script_output()
    empty_body["final_script"]["voiceover"] = ""
    with pytest.raises(TaskItemOutputValidationError, match="media_script_batch_item_output"):
        validate_output_payload("media_script_batch_item_output", empty_body)

    interrupted = valid_script_output()
    interrupted["hermes_agent_result"]["status"] = "interrupted"
    with pytest.raises(TaskItemOutputValidationError, match="media_script_batch_item_output"):
        validate_output_payload("media_script_batch_item_output", interrupted)


def test_non_json_script_fails_only_that_item_and_persists_status(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'script-batch-non-json.db'}",
        )
        reset_engine_for_test()
        await init_db()

        async def fake_run_scheduler_for_item(*, item, **_kwargs):
            if item.item_key == "run-item-bad":
                return "not-json-script-output", {"total_tokens": 3}
            return json.dumps(valid_script_output(item.input_payload_json["topic"])), {
                "total_tokens": 12
            }

        monkeypatch.setattr(
            batch_handler,
            "_run_scheduler_for_item",
            fake_run_scheduler_for_item,
        )

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        headers = {"X-User-Id": "script-batch-test-user"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/task-manager/run",
                headers=headers,
                json={
                    "task_type": "media.script.batch_generate",
                    "title": "Generate scripts for today's run",
                    "stream": False,
                    "input_payload": batch_input(
                        items=[daily_item("good"), daily_item("bad")]
                    ),
                },
            )
            assert response.status_code == 200
            task = response.json()["task"]
            assert task["status"] == "succeeded"
            assert task["result_payload_json"]["structured"]["summary"] == {
                "total": 2,
                "succeeded": 1,
                "failed": 1,
                "skipped": 0,
            }

            items_response = await client.get(
                f"/task-manager/tasks/{task['id']}/items",
                headers=headers,
            )
            assert items_response.status_code == 200
            by_key = {
                item["item_key"]: item for item in items_response.json()["items"]
            }
            assert by_key["run-item-good"]["status"] == "succeeded"
            assert (
                by_key["run-item-good"]["result_payload_json"]["result"]["final_script"][
                    "voiceover"
                ]
            )
            assert by_key["run-item-bad"]["status"] == "failed"
            assert (
                by_key["run-item-bad"]["error_payload_json"]["type"]
                == "TaskItemOutputValidationError"
            )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_script_batch_retries_one_failed_attempt_without_repeating_other_items(
    tmp_path,
    monkeypatch,
):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'script-batch-retry.db'}",
        )
        reset_engine_for_test()
        await init_db()
        attempts: dict[str, int] = {}

        async def fake_run_scheduler_for_item(*, item, **_kwargs):
            attempts[item.item_key] = attempts.get(item.item_key, 0) + 1
            if item.item_key == "run-item-retry" and attempts[item.item_key] == 1:
                raise RuntimeError("synthetic transient failure")
            return json.dumps(valid_script_output(item.input_payload_json["topic"])), {}

        monkeypatch.setattr(
            batch_handler,
            "_run_scheduler_for_item",
            fake_run_scheduler_for_item,
        )

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/task-manager/run",
                headers={"X-User-Id": "script-batch-retry-user"},
                json={
                    "task_type": "media.script.batch_generate",
                    "title": "Retry one script item",
                    "stream": False,
                    "input_payload": batch_input(
                        items=[daily_item("retry"), daily_item("stable")],
                        retry_per_item=1,
                    ),
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["task"]["result_payload_json"]["structured"]["summary"] == {
            "total": 2,
            "succeeded": 2,
            "failed": 0,
            "skipped": 0,
        }
        assert attempts == {"run-item-retry": 2, "run-item-stable": 1}
        assert any(event["event_type"] == "item_retry" for event in body["events"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
