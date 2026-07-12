from __future__ import annotations

import pytest
from task_manager.payload_schemas import TaskItemOutputValidationError, validate_input_payload, validate_output_payload
from task_manager.registry import get_task_definition


def test_content_analysis_batch_task_uses_item_scheduler_and_strict_schemas() -> None:
    definition = get_task_definition("analytics.douyin.content_analysis.batch")
    assert definition.handler == "batch_item_scheduler"
    assert definition.input_schema_name == "douyin_content_analysis_batch_input"
    assert definition.item_output_schema_name == "douyin_content_analysis_item_output"
    payload = validate_input_payload(definition.input_schema_name, {
        "account_id": "acct",
        "period": {"start_date": "2026-06-12", "end_date": "2026-07-11"},
        "failure_policy": "continue",
        "items": [{"id": "best:v1", "content_id": "v1", "rank_type": "best", "rank": 1, "evidence": {"play_count": 0}}],
    })
    assert payload["items"][0]["evidence"]["play_count"] == 0


def test_item_output_requires_evidence_reason_and_rejects_unknown_fields() -> None:
    with pytest.raises(TaskItemOutputValidationError):
        validate_output_payload("douyin_content_analysis_item_output", {
            "content_id": "v1", "evidence_based_reason": [], "invented": True,
        })


def test_legacy_report_bottom_default_supports_three_items() -> None:
    from task_manager.adapters.legacy_douyin import build_douyin_account_report_payload

    result = build_douyin_account_report_payload(
        original={"account_id": "acct"}, overview={"account": {}, "metrics": {}},
        contents_response={"items": [{"id": f"v{i}", "play_count": i} for i in range(6)]},
        base_url="http://127.0.0.1:8010", content_limit=50,
    )
    assert len(result["low_contents"]) == 3
