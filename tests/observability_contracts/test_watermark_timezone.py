from __future__ import annotations

from datetime import datetime, timezone

from model_observability.query import _watermark
from model_observability.snapshot import SnapshotPage, StoredSnapshot
from task_manager.observability_internal.models import QuerySnapshotEntity
from task_manager.observability_internal.snapshots import SnapshotStore


def test_task_watermark_restores_utc_to_database_datetimes() -> None:
    row = QuerySnapshotEntity(
        handle="hwm_task_timezone_test",
        query_snapshot_id="qs_task",
        resource_kind="TASK",
        snapshot_mode="MATERIALIZED_RESULT_SET",
        scope_hash="a" * 64,
        filter_hash="b" * 64,
        snapshot_to=datetime(2026, 8, 7, 8, 44, 11),
        expires_at=datetime(2026, 8, 7, 8, 59, 11),
        max_ingested_at=datetime(2026, 8, 7, 8, 20, 57),
    )

    watermark = SnapshotStore.watermark(row)

    assert watermark.snapshot_to.tzinfo is timezone.utc
    assert watermark.expires_at.tzinfo is timezone.utc
    assert watermark.max_ingested_at is not None
    assert watermark.max_ingested_at.tzinfo is timezone.utc


def test_model_watermark_restores_utc_to_database_datetimes() -> None:
    snapshot = StoredSnapshot[object](
        query_snapshot_id="qs_model",
        high_watermark_handle="hwm_model_timezone_test",
        scope_key="scope",
        query_hash="query",
        rows=(),
        snapshot_to=datetime(2026, 8, 7, 8, 44, 11),
        created_at=datetime(2026, 8, 7, 8, 44, 11),
        expires_at=datetime(2026, 8, 7, 8, 59, 11),
        snapshot_mode="MATERIALIZED_RESULT_SET",
        max_ingested_at=datetime(2026, 8, 7, 8, 20, 57),
        max_event_id=None,
        max_sequence=None,
        data_through=datetime(2026, 8, 7, 8, 20, 57),
        cursor_signing_key="test-key",
    )
    page = SnapshotPage(rows=(), next_cursor=None, has_more=False, snapshot=snapshot)

    watermark = _watermark(page)

    assert watermark.snapshot_to.tzinfo is timezone.utc
    assert watermark.expires_at.tzinfo is timezone.utc
    assert watermark.max_ingested_at is not None
    assert watermark.max_ingested_at.tzinfo is timezone.utc
    assert watermark.data_through is not None
    assert watermark.data_through.tzinfo is timezone.utc
