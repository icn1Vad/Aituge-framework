from __future__ import annotations

from datetime import timedelta
from dataclasses import replace
from decimal import Decimal

import pytest
import model_observability.lifecycle as lifecycle_module

from model_observability.domain import (
    CostSource,
    InvocationMetrics,
    InvocationOutcome,
    ModelInvocationContext,
    ModelInvocationEventType,
    PrivacyMode,
    RouteType,
    ModelSnapshotError,
    utc_now,
)
from model_observability.lifecycle import ModelInvocationLifecycleService
from model_observability.query import (
    ModelEventFilters,
    ModelInvocationFilters,
    ModelInvocationQueryService,
    ModelQueryScope,
    ModelSummaryFilters,
)


_SENSITIVE_IDENTIFIER_VALUES = (
    f"eyJ{'A' * 8}.{'B' * 8}.{'C' * 8}",
    f"hwm_{'H' * 20}",
    f"cur_{'U' * 24}",
    f"crf_revfin_v1.payload.{'R' * 43}",
    f"qtk_qs_{'Q' * 43}.{'S' * 43}",
    f"cap.payload.{'C' * 43}",
    f"obs1.test.MODEL.{'O' * 20}.1.{'Z' * 43}",
    f"sk-proj-{'K' * 32}",
    "Bearer-leaked-credential",
    "Authorization-leaked-credential",
)
_SENSITIVE_IDENTIFIER_CASES = tuple(
    candidate
    for value in _SENSITIVE_IDENTIFIER_VALUES
    for candidate in (value, f"head-{value}-tail")
)


def _context(
    invocation_id: str,
    logical_call_id: str,
    attempt_no: int,
) -> ModelInvocationContext:
    return ModelInvocationContext(
        tenant_id="tenant-a",
        feature_code="contract.review",
        provider="provider-a",
        model_name="model-a",
        privacy_mode=PrivacyMode.STANDARD,
        route_type=RouteType.EXTERNAL,
        logical_call_id=logical_call_id,
        invocation_id=invocation_id,
        attempt_no=attempt_no,
        task_id="task-a",
        run_id="run-a",
    )


async def _terminal(
    service: ModelInvocationLifecycleService,
    context: ModelInvocationContext,
    *,
    dispatch: str,
    outcome: InvocationOutcome,
    amount: str | None = None,
    currency: str | None = None,
) -> None:
    row = await service.start_invocation(context)
    if dispatch == "DISPATCHED":
        await service.mark_dispatched(row.invocation_id)
    elif dispatch == "DISPATCH_UNKNOWN":
        await service.mark_dispatch_unknown(row.invocation_id)
    metrics = InvocationMetrics(
        input_tokens=10 if dispatch == "DISPATCHED" else None,
        output_tokens=2 if dispatch == "DISPATCHED" else None,
        latency_ms=100 if dispatch == "DISPATCHED" else None,
        cost_amount=Decimal(amount) if amount is not None else None,
        cost_currency=currency,
        cost_source=CostSource.PROVIDER if amount is not None else None,
    )
    await service.terminate(
        row.invocation_id,
        event_type=(
            ModelInvocationEventType.SUCCEEDED
            if outcome is InvocationOutcome.SUCCESS
            else ModelInvocationEventType.FAILED
        ),
        outcome=outcome,
        error_code=None if outcome is InvocationOutcome.SUCCESS else "MODEL_FAILED",
        metrics=metrics,
    )


@pytest.mark.asyncio
async def test_summary_keeps_currencies_and_dispatch_denominators_separate(
    isolated_model_store,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    await _terminal(
        lifecycle,
        _context("inv-a1", "logical-a", 1),
        dispatch="DISPATCHED",
        outcome=InvocationOutcome.FAILURE,
        amount="1.00",
        currency="CNY",
    )
    await _terminal(
        lifecycle,
        _context("inv-a2", "logical-a", 2),
        dispatch="DISPATCHED",
        outcome=InvocationOutcome.SUCCESS,
        amount="2.00",
        currency="CNY",
    )
    await _terminal(
        lifecycle,
        _context("inv-b1", "logical-b", 1),
        dispatch="DISPATCHED",
        outcome=InvocationOutcome.SUCCESS,
        amount="5.00",
        currency="USD",
    )
    await _terminal(
        lifecycle,
        _context("inv-c1", "logical-c", 1),
        dispatch="NOT_DISPATCHED",
        outcome=InvocationOutcome.FAILURE,
    )
    await _terminal(
        lifecycle,
        _context("inv-d1", "logical-d", 1),
        dispatch="DISPATCH_UNKNOWN",
        outcome=InvocationOutcome.FAILURE,
    )
    query = ModelInvocationQueryService(isolated_model_store)
    now = utc_now()

    summary = await query.summary(
        scope=ModelQueryScope(("tenant-a",), "scope-a"),
        from_time=now - timedelta(days=1),
        to_time=now + timedelta(minutes=1),
        filters=ModelSummaryFilters(),
    )

    assert summary.logical_call_count == 4
    assert summary.dispatch_attempt_count == 5
    assert summary.dispatched_request_count == 3
    assert summary.not_dispatched_count == 1
    assert summary.dispatch_unknown_count == 1
    assert summary.active_attempt_count == 0
    assert summary.logical_success_rate == pytest.approx(0.5)
    assert summary.attempt_success_rate == pytest.approx(2 / 3)
    assert summary.retry_rate == pytest.approx(1 / 3)
    assert [(item.currency, item.amount) for item in summary.costs] == [
        ("CNY", 3.0),
        ("USD", 5.0),
    ]


@pytest.mark.asyncio
async def test_running_attempt_is_active_not_proven_not_dispatched(
    isolated_model_store,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    await lifecycle.start_invocation(_context("inv-active", "logical-active", 1))
    query = ModelInvocationQueryService(isolated_model_store)
    now = utc_now()

    summary = await query.summary(
        scope=ModelQueryScope(("tenant-a",), "scope-a"),
        from_time=now - timedelta(days=1),
        to_time=now + timedelta(minutes=1),
        filters=ModelSummaryFilters(),
    )

    assert summary.dispatch_attempt_count == 1
    assert summary.active_attempt_count == 1
    assert summary.not_dispatched_count == 0
    assert summary.dispatched_request_count == 0
    assert summary.dispatch_unknown_count == 0


@pytest.mark.asyncio
async def test_materialized_invocation_snapshot_does_not_drift_between_pages(
    isolated_model_store,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    old = utc_now() - timedelta(minutes=10)
    newer = await lifecycle.start_invocation(
        _context("inv-new", "logical-new", 1),
        occurred_at=old + timedelta(minutes=1),
    )
    older = await lifecycle.start_invocation(
        _context("inv-old", "logical-old", 1),
        occurred_at=old,
    )
    query = ModelInvocationQueryService(isolated_model_store)
    snapshot_to = utc_now() + timedelta(seconds=1)
    scope = ModelQueryScope(("tenant-a",), "scope-a")
    first = await query.list_invocations(
        scope=scope,
        from_time=old - timedelta(minutes=1),
        to_time=snapshot_to,
        snapshot_to=snapshot_to,
        filters=ModelInvocationFilters(),
        high_watermark=None,
        cursor=None,
        limit=1,
    )
    assert [item.invocation_id for item in first.data] == [newer.invocation_id]
    assert first.page.has_more is True

    await lifecycle.terminate(
        older.invocation_id,
        event_type=ModelInvocationEventType.FAILED,
        outcome=InvocationOutcome.FAILURE,
        error_code="LATE_STATE_CHANGE",
    )
    second = await query.list_invocations(
        scope=scope,
        from_time=old - timedelta(minutes=1),
        to_time=snapshot_to,
        snapshot_to=snapshot_to,
        filters=ModelInvocationFilters(),
        high_watermark=first.watermark.high_watermark_handle,
        cursor=first.page.next_cursor,
        limit=1,
    )
    assert [item.invocation_id for item in second.data] == [older.invocation_id]
    assert second.data[0].lifecycle_status.value == "RUNNING"
    assert second.data[0].outcome is None


@pytest.mark.asyncio
async def test_event_list_is_append_only_sorted_and_tenant_scoped(
    isolated_model_store,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    row = await lifecycle.start_invocation(_context("inv-event", "logical-event", 1))
    await lifecycle.mark_dispatched(row.invocation_id)
    query = ModelInvocationQueryService(isolated_model_store)
    now = utc_now()

    result = await query.list_events(
        scope=ModelQueryScope(("tenant-a",), "scope-a"),
        from_time=now - timedelta(hours=1),
        to_time=now + timedelta(minutes=1),
        snapshot_to=now + timedelta(minutes=1),
        filters=ModelEventFilters(invocation_id=row.invocation_id),
        high_watermark=None,
        cursor=None,
        limit=100,
    )

    assert [item.event_type for item in result.data] == [
        "MODEL_INVOCATION_DISPATCHED",
        "MODEL_INVOCATION_STARTED",
    ]
    assert {item.tenant_id for item in result.data} == {"tenant-a"}


@pytest.mark.asyncio
async def test_snapshot_scope_binds_canonical_tenants_and_authorized_claims(
    isolated_model_store,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    await lifecycle.start_invocation(_context("inv-scope-a", "logical-scope-a", 1))
    await lifecycle.start_invocation(
        replace(
            _context("inv-scope-b", "logical-scope-b", 1),
            tenant_id="tenant-b",
        )
    )
    query = ModelInvocationQueryService(isolated_model_store)
    now = utc_now()
    common = {
        "from_time": now - timedelta(hours=1),
        "to_time": now + timedelta(minutes=1),
        "snapshot_to": now + timedelta(minutes=1),
        "filters": ModelInvocationFilters(),
        "cursor": None,
        "limit": 100,
    }
    first = await query.list_invocations(
        scope=ModelQueryScope(
            ("tenant-a",),
            "service-observability",
            "test",
            ("models:read",),
            ("actor:admin",),
        ),
        high_watermark=None,
        **common,
    )

    with pytest.raises(ModelSnapshotError):
        await query.list_invocations(
            scope=ModelQueryScope(
                ("tenant-b",),
                "service-observability",
                "test",
                ("models:read",),
                ("actor:admin",),
            ),
            high_watermark=first.watermark.high_watermark_handle,
            **common,
        )

    both = await query.list_invocations(
        scope=ModelQueryScope(
            ("tenant-b", "tenant-a"),
            "service-observability",
            "test",
            ("models:read",),
            ("actor:admin",),
        ),
        high_watermark=None,
        **common,
    )
    reordered = await query.list_invocations(
        scope=ModelQueryScope(
            ("tenant-a", "tenant-b"),
            "service-observability",
            "test",
            ("models:read",),
            ("actor:admin",),
        ),
        high_watermark=None,
        **common,
    )
    assert (
        both.watermark.high_watermark_handle
        == reordered.watermark.high_watermark_handle
    )


@pytest.mark.asyncio
async def test_query_rejects_empty_filter_and_naive_time(
    isolated_model_store,
) -> None:
    query = ModelInvocationQueryService(isolated_model_store)
    now = utc_now()
    common = {
        "scope": ModelQueryScope(("tenant-a",), "service-observability"),
        "to_time": now,
        "snapshot_to": now,
        "high_watermark": None,
        "cursor": None,
        "limit": 100,
    }
    with pytest.raises(ValueError, match="task_id must not be empty"):
        await query.list_invocations(
            from_time=now - timedelta(minutes=1),
            filters=ModelInvocationFilters(task_id=""),
            **common,
        )
    with pytest.raises(ValueError, match="from must include a timezone"):
        await query.list_invocations(
            from_time=(now - timedelta(minutes=1)).replace(tzinfo=None),
            filters=ModelInvocationFilters(),
            **common,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_value", _SENSITIVE_IDENTIFIER_CASES)
async def test_invocation_query_rejects_sensitive_ordinary_filter_before_snapshot(
    isolated_model_store,
    sensitive_value: str,
) -> None:
    now = utc_now()
    query = ModelInvocationQueryService(isolated_model_store)

    with pytest.raises(
        ValueError,
        match="model_name cannot contain a token or capability value",
    ):
        await query.list_invocations(
            scope=ModelQueryScope(("tenant-a",), "scope-a"),
            from_time=now - timedelta(minutes=1),
            to_time=now,
            snapshot_to=now,
            filters=ModelInvocationFilters(model_name=sensitive_value),
            high_watermark=None,
            cursor=None,
            limit=100,
        )


@pytest.mark.asyncio
async def test_event_summary_and_detail_reject_sensitive_identifiers(
    isolated_model_store,
) -> None:
    now = utc_now()
    query = ModelInvocationQueryService(isolated_model_store)

    with pytest.raises(
        ValueError,
        match="run_id cannot contain a token or capability value",
    ):
        await query.list_events(
            scope=ModelQueryScope(("tenant-a",), "scope-a"),
            from_time=now - timedelta(minutes=1),
            to_time=now,
            snapshot_to=now,
            filters=ModelEventFilters(
                run_id=f"cap.payload.{'C' * 43}",
            ),
            high_watermark=None,
            cursor=None,
            limit=100,
        )
    with pytest.raises(
        ValueError,
        match="model_name cannot contain a token or capability value",
    ):
        await query.summary(
            scope=ModelQueryScope(("tenant-a",), "scope-a"),
            from_time=now - timedelta(minutes=1),
            to_time=now,
            filters=ModelSummaryFilters(
                model_name=f"qtk_qs_{'Q' * 43}.{'S' * 43}",
            ),
        )
    with pytest.raises(
        ValueError,
        match="invocation_id cannot contain a token or capability value",
    ):
        await query.get_invocation(
            scope=ModelQueryScope(("tenant-a",), "scope-a"),
            invocation_id=f"hwm_{'H' * 20}",
        )


@pytest.mark.asyncio
async def test_query_allows_incomplete_non_capability_identifier_shapes(
    isolated_model_store,
) -> None:
    now = utc_now()
    query = ModelInvocationQueryService(isolated_model_store)

    invocations = await query.list_invocations(
        scope=ModelQueryScope(("tenant-a",), "scope-a"),
        from_time=now - timedelta(minutes=1),
        to_time=now,
        snapshot_to=now,
        filters=ModelInvocationFilters(model_name="hwm_status"),
        high_watermark=None,
        cursor=None,
        limit=100,
    )
    events = await query.list_events(
        scope=ModelQueryScope(("tenant-a",), "scope-a"),
        from_time=now - timedelta(minutes=1),
        to_time=now,
        snapshot_to=now,
        filters=ModelEventFilters(run_id="cap.short.short"),
        high_watermark=None,
        cursor=None,
        limit=100,
    )
    summary = await query.summary(
        scope=ModelQueryScope(("tenant-a",), "scope-a"),
        from_time=now - timedelta(minutes=1),
        to_time=now,
        filters=ModelSummaryFilters(model_name="obs1.test.MODEL.short.1.short"),
    )

    assert invocations.data == []
    assert events.data == []
    assert summary.dispatch_attempt_count == 0


@pytest.mark.asyncio
async def test_event_watermark_pairs_sequence_and_id_but_maximizes_ingestion_independently(
    isolated_model_store,
    monkeypatch,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    now = utc_now()
    higher_ingestion = now - timedelta(minutes=5)
    lower_ingestion = now - timedelta(minutes=10)
    monkeypatch.setattr(lifecycle_module, "utc_now", lambda: higher_ingestion)
    first = await lifecycle.start_invocation(
        _context("inv-watermark-first", "logical-watermark-first", 1),
        occurred_at=now - timedelta(minutes=30),
    )
    monkeypatch.setattr(lifecycle_module, "utc_now", lambda: lower_ingestion)
    second = await lifecycle.start_invocation(
        _context("inv-watermark-second", "logical-watermark-second", 1),
        occurred_at=now - timedelta(minutes=20),
    )
    events = [
        *(await lifecycle.get_events(first.invocation_id)),
        *(await lifecycle.get_events(second.invocation_id)),
    ]
    causal_max = max(events, key=lambda item: item.server_sequence)
    ingestion_max = max(events, key=lambda item: item.ingested_at)
    assert causal_max.event_id != ingestion_max.event_id
    result = await ModelInvocationQueryService(
        isolated_model_store
    ).list_events(
        scope=ModelQueryScope(("tenant-a",), "service-observability"),
        from_time=now - timedelta(hours=1),
        to_time=now,
        snapshot_to=now,
        filters=ModelEventFilters(),
        high_watermark=None,
        cursor=None,
        limit=100,
    )

    assert result.watermark.max_sequence == causal_max.server_sequence
    assert result.watermark.max_event_id == causal_max.event_id
    assert result.watermark.max_ingested_at == ingestion_max.ingested_at.replace(
        tzinfo=result.watermark.max_ingested_at.tzinfo,
    )


@pytest.mark.asyncio
async def test_logical_cohort_uses_first_attempt_bucket_once(
    isolated_model_store,
) -> None:
    lifecycle = ModelInvocationLifecycleService(isolated_model_store)
    first_started = (utc_now() - timedelta(hours=4)).replace(
        minute=10,
        second=0,
        microsecond=0,
    )
    first = await lifecycle.start_invocation(
        _context("inv-cohort-1", "logical-cohort", 1),
        occurred_at=first_started,
    )
    await lifecycle.mark_dispatched(
        first.invocation_id,
        occurred_at=first_started + timedelta(seconds=1),
    )
    await lifecycle.terminate(
        first.invocation_id,
        event_type=ModelInvocationEventType.FAILED,
        outcome=InvocationOutcome.FAILURE,
        error_code="MODEL_FAILED",
        occurred_at=first_started + timedelta(seconds=2),
    )
    second_started = first_started + timedelta(hours=2)
    second = await lifecycle.start_invocation(
        replace(
            _context("inv-cohort-2", "logical-cohort", 2),
            fallback_from_invocation_id=first.invocation_id,
        ),
        occurred_at=second_started,
    )
    await lifecycle.mark_dispatched(
        second.invocation_id,
        occurred_at=second_started + timedelta(seconds=1),
    )
    await lifecycle.terminate(
        second.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
        occurred_at=second_started + timedelta(seconds=2),
    )
    query = ModelInvocationQueryService(isolated_model_store)
    scope = ModelQueryScope(("tenant-a",), "service-observability")

    retry_only_window = await query.summary(
        scope=scope,
        from_time=first_started + timedelta(hours=1),
        to_time=second_started + timedelta(minutes=1),
        filters=ModelSummaryFilters(),
    )
    assert retry_only_window.dispatch_attempt_count == 1
    assert retry_only_window.logical_call_count == 0

    full_cohort = await query.summary(
        scope=scope,
        from_time=first_started - timedelta(minutes=1),
        to_time=second_started + timedelta(minutes=1),
        filters=ModelSummaryFilters(),
    )
    assert full_cohort.logical_call_count == 1
    assert full_cohort.dispatch_attempt_count == 2
    assert len(full_cohort.time_series) == 1
    assert full_cohort.time_series[0].bucket_start.hour == first_started.hour
    assert full_cohort.time_series[0].logical_call_count == 1
    assert full_cohort.time_series[0].dispatch_attempt_count == 2
    assert full_cohort.time_series[0].success_rate == 1.0
