from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Any

from .domain import (
    DispatchStatus,
    InvocationLifecycleStatus,
    InvocationNotFoundError,
    ModelSnapshotError,
    contains_sensitive_identifier_value,
    PrivacyMode,
    RouteType,
    utc_now,
)
from .entities import ModelInvocationEventEntity, ModelInvocationProjectionEntity
from .lifecycle import SessionContextFactory
from .repository import ModelInvocationRepository
from .schemas import (
    BreakdownItem,
    CurrencyAmount,
    InternalModelEventList,
    InternalModelInvocation,
    InternalModelInvocationDetail,
    InternalModelInvocationEvent,
    InternalModelInvocationList,
    InternalModelSummary,
    ModelTimeSeriesPoint,
    PageInfo,
    Percentiles,
    SourceWatermark,
    metadata_fields,
)
from .snapshot import ModelSnapshotStore, SnapshotPage, stable_query_hash


@dataclass(frozen=True, slots=True)
class ModelQueryScope:
    tenant_ids: tuple[str, ...]
    scope_key: str
    environment: str = "default"
    permission_claims: tuple[str, ...] = ()
    scope_claims: tuple[str, ...] = ()
    all_tenants: bool = False

    def validate(self) -> None:
        if self.all_tenants:
            if self.tenant_ids:
                raise ValueError("all-tenant model scope cannot carry tenant ids")
        elif not self.tenant_ids or any(
            not isinstance(item, str) or not item.strip()
            for item in self.tenant_ids
        ):
            raise ValueError("model query scope requires at least one tenant")
        if not isinstance(self.scope_key, str) or not self.scope_key.strip():
            raise ValueError("model query authorized principal is required")
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise ValueError("model query environment is required")
        for name, claims in (
            ("permission_claims", self.permission_claims),
            ("scope_claims", self.scope_claims),
        ):
            if any(
                not isinstance(item, str) or not item.strip()
                for item in claims
            ):
                raise ValueError(
                    f"model query {name} must contain non-empty strings"
                )

    @property
    def canonical_tenant_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.tenant_ids)))

    @property
    def snapshot_scope_key(self) -> str:
        """Derive identity from server-authorized scope attributes."""

        self.validate()
        return stable_query_hash(
            {
                "authorized_principal": self.scope_key,
                "environment": self.environment,
                "tenant_ids": self.canonical_tenant_ids,
                "all_tenants": self.all_tenants,
                "permission_claims": tuple(
                    sorted(set(self.permission_claims))
                ),
                "scope_claims": tuple(sorted(set(self.scope_claims))),
            }
        )


@dataclass(frozen=True, slots=True)
class ModelInvocationFilters:
    task_id: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    feature_code: str | None = None
    logical_call_id: str | None = None
    invocation_id: str | None = None
    provider: str | None = None
    model_pack_id: str | None = None
    model_name: str | None = None
    privacy_mode: str | None = None
    route_type: str | None = None
    lifecycle_status: str | None = None
    dispatch_status: str | None = None
    outcome: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ModelEventFilters:
    task_id: str | None = None
    run_id: str | None = None
    logical_call_id: str | None = None
    invocation_id: str | None = None
    event_id: str | None = None
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class ModelSummaryFilters:
    feature_code: str | None = None
    provider: str | None = None
    model_pack_id: str | None = None
    model_name: str | None = None
    privacy_mode: str | None = None
    route_type: str | None = None


class ModelInvocationQueryService:
    def __init__(
        self,
        session_factory: SessionContextFactory,
        *,
        snapshot_store: ModelSnapshotStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._snapshots = snapshot_store or ModelSnapshotStore(session_factory)

    async def list_invocations(
        self,
        *,
        scope: ModelQueryScope,
        from_time: datetime,
        to_time: datetime,
        snapshot_to: datetime,
        filters: ModelInvocationFilters,
        high_watermark: str | None,
        cursor: str | None,
        limit: int,
    ) -> InternalModelInvocationList:
        self._validate_common(scope, from_time, to_time, snapshot_to, limit)
        _validate_invocation_filters(filters)
        query_hash = stable_query_hash(
            {
                "kind": "invocation",
                "tenant_ids": scope.canonical_tenant_ids,
                "from": from_time,
                "to": to_time,
                "snapshot_to": snapshot_to,
                "filters": asdict(filters),
            }
        )
        if cursor and not high_watermark:
            raise ModelSnapshotError("cursor requires highWatermark")
        if high_watermark:
            page = await self._snapshots.page(
                high_watermark_handle=high_watermark,
                cursor=cursor,
                scope_key=scope.snapshot_scope_key,
                query_hash=query_hash,
                limit=limit,
            )
        else:
            async with self._session_factory() as session:
                rows = await ModelInvocationRepository(session).query_projections(
                    tenant_ids=scope.canonical_tenant_ids,
                    all_tenants=scope.all_tenants,
                    from_time=from_time,
                    to_time=to_time,
                    snapshot_to=snapshot_to,
                    limit=self._snapshots.max_rows + 1,
                    **asdict(filters),
                )
            summaries = [_projection_to_schema(item) for item in rows]
            snapshot = await self._snapshots.create_or_reuse(
                scope_key=scope.snapshot_scope_key,
                query_hash=query_hash,
                rows=summaries,
                snapshot_to=snapshot_to,
                snapshot_mode="MATERIALIZED_RESULT_SET",
                max_ingested_at=max(
                    (_aware(item.ingested_at) for item in rows),
                    default=None,
                ),
                max_event_id=None,
                max_sequence=None,
                data_through=max(
                    (_aware(item.started_at) for item in rows),
                    default=None,
                ),
            )
            page = await self._snapshots.first_page(snapshot, limit=limit)
        return InternalModelInvocationList(
            data=list(page.rows),
            page=PageInfo(
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                limit=limit,
            ),
            watermark=_watermark(page),
        )

    async def list_events(
        self,
        *,
        scope: ModelQueryScope,
        from_time: datetime,
        to_time: datetime,
        snapshot_to: datetime,
        filters: ModelEventFilters,
        high_watermark: str | None,
        cursor: str | None,
        limit: int,
    ) -> InternalModelEventList:
        self._validate_common(scope, from_time, to_time, snapshot_to, limit)
        _validate_event_filters(filters)
        query_hash = stable_query_hash(
            {
                "kind": "event",
                "tenant_ids": scope.canonical_tenant_ids,
                "from": from_time,
                "to": to_time,
                "snapshot_to": snapshot_to,
                "filters": asdict(filters),
            }
        )
        if cursor and not high_watermark:
            raise ModelSnapshotError("cursor requires highWatermark")
        if high_watermark:
            page = await self._snapshots.page(
                high_watermark_handle=high_watermark,
                cursor=cursor,
                scope_key=scope.snapshot_scope_key,
                query_hash=query_hash,
                limit=limit,
            )
        else:
            async with self._session_factory() as session:
                rows = await ModelInvocationRepository(session).query_events(
                    tenant_ids=scope.canonical_tenant_ids,
                    all_tenants=scope.all_tenants,
                    from_time=from_time,
                    to_time=to_time,
                    snapshot_to=snapshot_to,
                    limit=self._snapshots.max_rows + 1,
                    **asdict(filters),
                )
            events = [_event_to_schema(item) for item in rows]
            watermark_event = max(
                rows,
                key=lambda item: item.server_sequence,
                default=None,
            )
            snapshot = await self._snapshots.create_or_reuse(
                scope_key=scope.snapshot_scope_key,
                query_hash=query_hash,
                rows=events,
                snapshot_to=snapshot_to,
                snapshot_mode="APPEND_ONLY_HIGH_WATERMARK",
                max_ingested_at=max(
                    (_aware(item.ingested_at) for item in rows),
                    default=None,
                ),
                max_event_id=(
                    watermark_event.event_id
                    if watermark_event is not None
                    else None
                ),
                max_sequence=(
                    watermark_event.server_sequence
                    if watermark_event is not None
                    else None
                ),
                data_through=max(
                    (_aware(item.occurred_at) for item in rows),
                    default=None,
                ),
            )
            page = await self._snapshots.first_page(snapshot, limit=limit)
        return InternalModelEventList(
            data=list(page.rows),
            page=PageInfo(
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                limit=limit,
            ),
            watermark=_watermark(page),
        )

    async def get_event(
        self,
        *,
        scope: ModelQueryScope,
        event_id: str,
    ) -> InternalModelInvocationEvent:
        scope.validate()
        _validate_filter_value("event_id", event_id)
        async with self._session_factory() as session:
            event = await ModelInvocationRepository(session).get_event(event_id)
        if event is None or (
            not scope.all_tenants
            and event.tenant_id not in scope.canonical_tenant_ids
        ):
            raise InvocationNotFoundError(event_id)
        return _event_to_schema(event)

    async def get_invocation(
        self,
        *,
        scope: ModelQueryScope,
        invocation_id: str,
    ) -> InternalModelInvocationDetail:
        scope.validate()
        _validate_filter_value("invocation_id", invocation_id)
        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            projection = await repository.get_projection(invocation_id)
            if (
                projection is None
                or projection.tenant_id not in scope.canonical_tenant_ids
            ):
                raise InvocationNotFoundError(invocation_id)
            events = await repository.list_invocation_events(invocation_id)
        return InternalModelInvocationDetail(
            invocation=_projection_to_schema(projection),
            events=[_event_to_schema(item) for item in events],
        )

    async def summary(
        self,
        *,
        scope: ModelQueryScope,
        from_time: datetime,
        to_time: datetime,
        filters: ModelSummaryFilters,
    ) -> InternalModelSummary:
        scope.validate()
        _validate_time_window(from_time, to_time)
        _validate_summary_filters(filters)
        async with self._session_factory() as session:
            aggregates = await ModelInvocationRepository(session).aggregate_summary(
                tenant_ids=scope.canonical_tenant_ids,
                all_tenants=scope.all_tenants,
                from_time=from_time,
                to_time=to_time,
                feature_code=filters.feature_code,
                provider=filters.provider,
                model_pack_id=filters.model_pack_id,
                model_name=filters.model_name,
                privacy_mode=filters.privacy_mode,
                route_type=filters.route_type,
            )
        return _build_aggregate_summary(aggregates)

    @staticmethod
    def _validate_common(
        scope: ModelQueryScope,
        from_time: datetime,
        to_time: datetime,
        snapshot_to: datetime,
        limit: int,
    ) -> None:
        scope.validate()
        _validate_time_window(from_time, to_time)
        if snapshot_to.tzinfo is None or snapshot_to.utcoffset() is None:
            raise ValueError("snapshotTo must include a timezone")
        if _aware(snapshot_to) < _aware(to_time):
            raise ValueError("snapshotTo must be at or after to")
        if _aware(snapshot_to) > utc_now() + _QUERY_FUTURE_TOLERANCE:
            raise ValueError("snapshotTo exceeds the allowed future tolerance")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")


def _projection_to_schema(row: ModelInvocationProjectionEntity) -> InternalModelInvocation:
    return InternalModelInvocation(
        invocation_id=row.invocation_id,
        projection_version=row.projection_version,
        data_as_of=_aware(row.data_as_of),
        logical_call_id=row.logical_call_id,
        attempt_no=row.attempt_no,
        fallback_from_invocation_id=row.fallback_from_invocation_id,
        tenant_id=row.tenant_id,
        feature_code=row.feature_code,
        task_id=row.task_id,
        run_id=row.run_id,
        stage_id=row.stage_id,
        request_id=row.request_id,
        trace_id=row.trace_id,
        started_at=_aware(row.started_at),
        finished_at=_aware(row.finished_at) if row.finished_at else None,
        ingested_at=_aware(row.ingested_at),
        lifecycle_status=row.lifecycle_status,
        dispatch_status=row.dispatch_status,
        outcome=row.outcome,
        error_code=row.error_code,
        provider=row.provider,
        provider_request_id_hash=row.provider_request_id_hash,
        model_pack_id=row.model_pack_id,
        model_pack_version=row.model_pack_version,
        model_name=row.model_name,
        deployment_name=row.deployment_name,
        provider_region=row.provider_region,
        privacy_mode=row.privacy_mode,
        route_type=row.route_type,
        input_tokens=row.input_token_count,
        output_tokens=row.output_token_count,
        latency_ms=row.latency_ms,
        time_to_first_token_ms=row.time_to_first_token_ms,
        cost_amount=float(row.cost_amount) if row.cost_amount is not None else None,
        cost_currency=row.cost_currency,
        cost_source=row.cost_source,
        pricing_version=row.pricing_version,
        cost_calculated_at=(
            _aware(row.cost_calculated_at) if row.cost_calculated_at else None
        ),
    )


def _event_to_schema(row: ModelInvocationEventEntity) -> InternalModelInvocationEvent:
    return InternalModelInvocationEvent(
        event_id=row.event_id,
        event_type=row.event_type,
        schema_version=row.schema_version,
        logical_call_id=row.logical_call_id,
        invocation_id=row.invocation_id,
        attempt_no=row.attempt_no,
        fallback_from_invocation_id=row.fallback_from_invocation_id,
        tenant_id=row.tenant_id,
        feature_code=row.feature_code,
        task_id=row.task_id,
        run_id=row.run_id,
        stage_id=row.stage_id,
        request_id=row.request_id,
        trace_id=row.trace_id,
        occurred_at=_aware(row.occurred_at),
        ingested_at=_aware(row.ingested_at),
        dispatch_status=row.dispatch_status,
        provider=row.provider,
        provider_request_id_hash=row.provider_request_id_hash,
        model_pack_id=row.model_pack_id,
        model_pack_version=row.model_pack_version,
        model_name=row.model_name,
        deployment_name=row.deployment_name,
        provider_region=row.provider_region,
        privacy_mode=row.privacy_mode,
        route_type=row.route_type,
        input_tokens=row.input_token_count,
        output_tokens=row.output_token_count,
        latency_ms=row.latency_ms,
        time_to_first_token_ms=row.time_to_first_token_ms,
        cost_amount=float(row.cost_amount) if row.cost_amount is not None else None,
        cost_currency=row.cost_currency,
        cost_source=row.cost_source,
        pricing_version=row.pricing_version,
        cost_calculated_at=(
            _aware(row.cost_calculated_at) if row.cost_calculated_at else None
        ),
        outcome=row.outcome,
        error_code=row.error_code,
        metadata=metadata_fields(row.metadata_json),
    )


def _watermark(page: SnapshotPage[Any]) -> SourceWatermark:
    snapshot = page.snapshot
    return SourceWatermark(
        query_snapshot_id=snapshot.query_snapshot_id,
        snapshot_to=_aware(snapshot.snapshot_to),
        expires_at=_aware(snapshot.expires_at),
        snapshot_mode=snapshot.snapshot_mode,
        max_ingested_at=(
            _aware(snapshot.max_ingested_at) if snapshot.max_ingested_at else None
        ),
        max_event_id=snapshot.max_event_id,
        max_sequence=snapshot.max_sequence,
        high_watermark_handle=snapshot.high_watermark_handle,
        data_through=_aware(snapshot.data_through) if snapshot.data_through else None,
    )


def _build_summary(rows: list[ModelInvocationProjectionEntity]) -> InternalModelSummary:
    attempt_count = len(rows)
    logical_groups: dict[str, list[ModelInvocationProjectionEntity]] = defaultdict(list)
    for row in rows:
        logical_groups[row.logical_call_id].append(row)
    logical_count = len(logical_groups)
    completed_logical_groups = [
        group
        for group in logical_groups.values()
        if not any(
            item.lifecycle_status == InvocationLifecycleStatus.RUNNING.value
            for item in group
        )
        and any(
            item.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value
            for item in group
        )
    ]
    logical_successes = sum(
        any(
            item.dispatch_status == DispatchStatus.DISPATCHED.value
            and item.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value
            and item.outcome == "SUCCESS"
            for item in group
        )
        for group in completed_logical_groups
    )
    dispatched = [
        row for row in rows if row.dispatch_status == DispatchStatus.DISPATCHED.value
    ]
    not_dispatched = [
        row
        for row in rows
        if row.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value
        and row.dispatch_status == DispatchStatus.NOT_DISPATCHED.value
    ]
    dispatch_unknown = [
        row for row in rows if row.dispatch_status == DispatchStatus.DISPATCH_UNKNOWN.value
    ]
    active = [
        row
        for row in rows
        if row.lifecycle_status == InvocationLifecycleStatus.RUNNING.value
    ]
    dispatched_terminal = [
        row
        for row in dispatched
        if row.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value
    ]
    attempt_successes = sum(row.outcome == "SUCCESS" for row in dispatched_terminal)
    retry_eligible = {
        key: [
            item
            for item in group
            if item.dispatch_status
            in {DispatchStatus.DISPATCHED.value, DispatchStatus.DISPATCH_UNKNOWN.value}
        ]
        for key, group in logical_groups.items()
    }
    retry_denominator = sum(bool(group) for group in retry_eligible.values())
    retried = sum(len(group) > 1 for group in retry_eligible.values())
    costs: dict[str, Decimal] = defaultdict(Decimal)
    for row in dispatched:
        if row.cost_amount is not None and row.cost_currency:
            costs[row.cost_currency] += Decimal(row.cost_amount)
    input_tokens = sum(row.input_token_count or 0 for row in dispatched)
    output_tokens = sum(row.output_token_count or 0 for row in dispatched)
    latencies = [float(row.latency_ms) for row in dispatched if row.latency_ms is not None]
    ttfts = [
        float(row.time_to_first_token_ms)
        for row in dispatched
        if row.time_to_first_token_ms is not None
    ]
    return InternalModelSummary(
        logical_call_count=logical_count,
        dispatch_attempt_count=attempt_count,
        dispatched_request_count=len(dispatched),
        not_dispatched_count=len(not_dispatched),
        dispatch_unknown_count=len(dispatch_unknown),
        active_attempt_count=len(active),
        logical_success_rate=_ratio(
            logical_successes,
            len(completed_logical_groups),
        ),
        attempt_success_rate=_ratio(attempt_successes, len(dispatched_terminal)),
        retry_rate=_ratio(retried, retry_denominator),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        costs=[
            CurrencyAmount(currency=currency, amount=float(amount))
            for currency, amount in sorted(costs.items())
        ],
        latency_percentiles=_percentiles(latencies),
        ttft_percentiles=_percentiles(ttfts),
        time_series=_time_series(rows),
        error_breakdown=_breakdown(
            row.error_code
            for row in rows
            if row.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value
            and row.error_code
        ),
        provider_breakdown=_breakdown(row.provider for row in dispatched),
        model_breakdown=_breakdown(row.model_name for row in dispatched),
        route_type_breakdown=_breakdown(row.route_type for row in rows),
        privacy_mode_breakdown=_breakdown(row.privacy_mode for row in rows),
    )


def _build_aggregate_summary(aggregates: dict[str, object]) -> InternalModelSummary:
    main = dict(aggregates["main"])
    logical = dict(aggregates["logical"])
    breakdowns = dict(aggregates["breakdowns"])

    def count(name: str, source: dict[str, Any] = main) -> int:
        return int(source.get(name) or 0)

    def breakdown(name: str) -> list[BreakdownItem]:
        rows = list(breakdowns.get(name) or [])
        total = sum(int(dict(row).get("count") or 0) for row in rows)
        return [
            BreakdownItem(
                key=str(dict(row)["key"]),
                count=int(dict(row)["count"]),
                ratio=_ratio(int(dict(row)["count"]), total),
            )
            for row in rows
        ]

    def percentiles(name: str) -> Percentiles | None:
        value = aggregates.get(name)
        return Percentiles(**dict(value)) if value else None

    time_series = []
    for raw in list(aggregates.get("time_series") or []):
        row = dict(raw)
        denominator = int(row.get("success_denominator") or 0)
        success_count = int(row.get("success_count") or 0)
        time_series.append(
            ModelTimeSeriesPoint(
                bucket_start=_summary_bucket(row["bucket_start"]),
                logical_call_count=int(row.get("logical_call_count") or 0),
                dispatch_attempt_count=int(row.get("dispatch_attempt_count") or 0),
                dispatched_request_count=int(
                    row.get("dispatched_request_count") or 0
                ),
                success_rate=_ratio(success_count, denominator),
                p95_latency_ms=float(row.get("p95_latency_ms") or 0.0),
            )
        )

    return InternalModelSummary(
        logical_call_count=count("logical_call_count", logical),
        dispatch_attempt_count=count("dispatch_attempt_count"),
        dispatched_request_count=count("dispatched_request_count"),
        not_dispatched_count=count("not_dispatched_count"),
        dispatch_unknown_count=count("dispatch_unknown_count"),
        active_attempt_count=count("active_attempt_count"),
        logical_success_rate=_ratio(
            count("logical_success_count", logical),
            count("logical_terminal_count", logical),
        ),
        attempt_success_rate=_ratio(
            count("attempt_success_count"),
            count("attempt_success_denominator"),
        ),
        retry_rate=_ratio(
            count("retried_count", logical),
            count("retry_denominator", logical),
        ),
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        costs=[
            CurrencyAmount(
                currency=str(dict(row)["currency"]),
                amount=float(dict(row)["amount"]),
            )
            for row in list(aggregates.get("costs") or [])
        ],
        latency_percentiles=percentiles("latency_percentiles"),
        ttft_percentiles=percentiles("ttft_percentiles"),
        time_series=time_series,
        error_breakdown=breakdown("error"),
        provider_breakdown=breakdown("provider"),
        model_breakdown=breakdown("model"),
        route_type_breakdown=breakdown("route_type"),
        privacy_mode_breakdown=breakdown("privacy_mode"),
    )


def _summary_bucket(value: object) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    return _aware(parsed)




def _breakdown(values) -> list[BreakdownItem]:
    counts = Counter(values)
    total = sum(counts.values())
    return [
        BreakdownItem(key=key, count=count, ratio=_ratio(count, total))
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _percentiles(values: list[float]) -> Percentiles | None:
    if not values:
        return None
    ordered = sorted(values)
    return Percentiles(
        p50=_percentile(ordered, 0.50),
        p95=_percentile(ordered, 0.95),
        p99=_percentile(ordered, 0.99),
    )


def _percentile(ordered: list[float], quantile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _time_series(
    rows: list[ModelInvocationProjectionEntity],
) -> list[ModelTimeSeriesPoint]:
    logical_groups: dict[
        str,
        list[ModelInvocationProjectionEntity],
    ] = defaultdict(list)
    for row in rows:
        logical_groups[row.logical_call_id].append(row)

    buckets: dict[
        datetime,
        list[list[ModelInvocationProjectionEntity]],
    ] = defaultdict(list)
    for group in logical_groups.values():
        first_started = min(_aware(item.started_at) for item in group)
        bucket = first_started.replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        buckets[bucket].append(group)

    result: list[ModelTimeSeriesPoint] = []
    for bucket, groups in sorted(buckets.items()):
        items = [item for group in groups for item in group]
        dispatched = [
            item
            for item in items
            if item.dispatch_status == DispatchStatus.DISPATCHED.value
        ]
        completed_groups = [
            group
            for group in groups
            if not any(
                item.lifecycle_status == InvocationLifecycleStatus.RUNNING.value
                for item in group
            )
            and any(
                item.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value
                for item in group
            )
        ]
        success = sum(
            any(
                item.dispatch_status == DispatchStatus.DISPATCHED.value
                and item.outcome == "SUCCESS"
                for item in group
            )
            for group in completed_groups
        )
        latencies = [
            float(item.latency_ms)
            for item in dispatched
            if item.latency_ms is not None
        ]
        p95 = _percentile(sorted(latencies), 0.95) if latencies else 0.0
        result.append(
            ModelTimeSeriesPoint(
                bucket_start=bucket,
                logical_call_count=len(groups),
                dispatch_attempt_count=len(items),
                dispatched_request_count=len(dispatched),
                success_rate=_ratio(success, len(completed_groups)),
                p95_latency_ms=p95,
            )
        )
    return result

def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_MAX_QUERY_WINDOW = timedelta(days=31)
_QUERY_FUTURE_TOLERANCE = timedelta(minutes=5)
_SAFE_FILTER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_STABLE_CODE_FILTER = re.compile(r"^[A-Z][A-Z0-9_]{2,119}$")
_FILTER_MAX_LENGTHS = {
    "task_id": 80,
    "run_id": 80,
    "stage_id": 120,
    "feature_code": 120,
    "logical_call_id": 80,
    "invocation_id": 80,
    "provider": 80,
    "model_pack_id": 120,
    "model_name": 160,
    "error_code": 120,
    "event_type": 80,
}


def _validate_filter_value(name: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    if contains_sensitive_identifier_value(value):
        raise ValueError(f"{name} cannot contain a token or capability value")
    if len(value) > _FILTER_MAX_LENGTHS[name]:
        raise ValueError(f"{name} exceeds its maximum length")
    pattern = _STABLE_CODE_FILTER if name == "error_code" else _SAFE_FILTER
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} has an invalid format")


def _validate_invocation_filters(filters: ModelInvocationFilters) -> None:
    values = asdict(filters)
    for name in _FILTER_MAX_LENGTHS:
        if name != "event_type":
            _validate_filter_value(name, values.get(name))
    enum_values = {
        "privacy_mode": {item.value for item in PrivacyMode},
        "route_type": {item.value for item in RouteType},
        "lifecycle_status": {item.value for item in InvocationLifecycleStatus},
        "dispatch_status": {item.value for item in DispatchStatus},
        "outcome": {
            "SUCCESS",
            "FAILURE",
            "DENIED",
            "CANCELLED",
            "TIMEOUT",
            "PARTIAL",
            "UNKNOWN",
            "ABANDONED",
        },
    }
    for name, allowed in enum_values.items():
        value = values[name]
        if value is not None and value not in allowed:
            raise ValueError(f"{name} is invalid")


def _validate_event_filters(filters: ModelEventFilters) -> None:
    values = asdict(filters)
    for name in (
        "task_id",
        "run_id",
        "logical_call_id",
        "invocation_id",
        "event_id",
        "event_type",
    ):
        _validate_filter_value(name, values[name])
    if (
        filters.event_type is not None
        and filters.event_type not in {
            "MODEL_INVOCATION_STARTED",
            "MODEL_INVOCATION_DISPATCHED",
            "MODEL_INVOCATION_DISPATCH_UNKNOWN",
            "MODEL_INVOCATION_SUCCEEDED",
            "MODEL_INVOCATION_FAILED",
            "MODEL_INVOCATION_VALIDATION_FAILED",
            "MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED",
            "MODEL_INVOCATION_OUTCOME_UNKNOWN",
            "MODEL_INVOCATION_ABANDONED",
        }
    ):
        raise ValueError("event_type is invalid")


def _validate_summary_filters(filters: ModelSummaryFilters) -> None:
    values = asdict(filters)
    for name in (
        "feature_code",
        "provider",
        "model_pack_id",
        "model_name",
    ):
        _validate_filter_value(name, values[name])
    for name, allowed in (
        ("privacy_mode", {item.value for item in PrivacyMode}),
        ("route_type", {item.value for item in RouteType}),
    ):
        value = values[name]
        if value is not None and value not in allowed:
            raise ValueError(f"{name} is invalid")


def _validate_time_window(from_time: datetime, to_time: datetime) -> None:
    for name, value in (("from", from_time), ("to", to_time)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
    aware_from = _aware(from_time)
    aware_to = _aware(to_time)
    if aware_from > aware_to:
        raise ValueError("from must not be after to")
    if aware_to - aware_from > _MAX_QUERY_WINDOW:
        raise ValueError("query window exceeds 31 days")
    if aware_to > utc_now() + _QUERY_FUTURE_TOLERANCE:
        raise ValueError("to exceeds the allowed future tolerance")
