from task_manager.observability_internal.event_registry import task_metadata_fields


def test_dropped_agent_delta_accepts_stream_whitespace_without_projection() -> None:
    assert task_metadata_fields("agent_delta", 1, {"delta": "\n\t"}) == []


def test_legacy_result_snapshot_is_accepted_without_exposing_preview() -> None:
    assert task_metadata_fields(
        "result_snapshot",
        1,
        {"original_chars": 123, "preview": "合同正文预览", "truncated": True},
    ) == []


def test_current_result_snapshot_keeps_only_safe_artifact_identifier() -> None:
    fields = task_metadata_fields(
        "result_snapshot",
        1,
        {"artifact_id": "artifact-1", "result": {"private": "content"}},
    )

    assert [field.model_dump(by_alias=True) for field in fields] == [
        {"key": "artifact_id", "value": "artifact-1", "masked": False}
    ]
