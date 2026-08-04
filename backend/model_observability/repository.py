from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .entities import (
    ModelInvocationEventEntity,
    ModelInvocationProjectionEntity,
)


class ModelInvocationRepository:
    """Persistence operations for the append-only ledger and its rebuildable projection."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def acquire_projection_write_lock(self) -> None:
        """Prevent projection rebuild from racing any ledger/projection write."""

        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock_shared(1827003)")
            )
        elif dialect != "sqlite":  # pragma: no cover
            raise RuntimeError(f"Unsupported model observability dialect: {dialect}")

    async def lock_invocation(self, invocation_id: str) -> None:
        """Serialize competing dispatch/finalize calls for one invocation."""

        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"invocation:{invocation_id}"},
            )
        elif dialect == "sqlite":
            await self.session.execute(text("BEGIN IMMEDIATE"))
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported model observability dialect: {dialect}")
    async def lock_logical_attempt(self, logical_call_id: str, attempt_no: int) -> None:
        """Serialize idempotency ownership before reading or appending facts."""

        dialect = self.session.get_bind().dialect.name
        lock_key = f"{logical_call_id}:{attempt_no}"
        if dialect == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
        elif dialect == "sqlite":
            # This is the first statement in the transaction in lifecycle.start.
            await self.session.execute(text("BEGIN IMMEDIATE"))
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported model observability dialect: {dialect}")
    async def next_server_sequence(self) -> int:
        """Allocate a causal sequence in the database, never from producer time."""

        bind = self.session.get_bind()
        dialect = bind.dialect.name
        if dialect == "postgresql":
            insert_sql = (
                "INSERT INTO tuge_model_observability_sequence "
                "(sequence_name, sequence_value) VALUES ('event', 0) "
                "ON CONFLICT (sequence_name) DO NOTHING"
            )
        elif dialect == "sqlite":
            insert_sql = (
                "INSERT OR IGNORE INTO tuge_model_observability_sequence "
                "(sequence_name, sequence_value) VALUES ('event', 0)"
            )
        else:  # pragma: no cover - PostgreSQL plus the SQLite test adapter only
            raise RuntimeError(f"Unsupported model observability dialect: {dialect}")
        await self.session.execute(text(insert_sql))
        value = (
            await self.session.execute(
                text(
                    "UPDATE tuge_model_observability_sequence "
                    "SET sequence_value = sequence_value + 1 "
                    "WHERE sequence_name = 'event' RETURNING sequence_value"
                )
            )
        ).scalar_one()
        return int(value)

    async def add_event(self, event: ModelInvocationEventEntity) -> None:
        self.session.add(event)
        if getattr(event, "server_sequence", None) is None:
            event.server_sequence = await self.next_server_sequence()
        await self.session.flush()

    async def add_projection(self, projection: ModelInvocationProjectionEntity) -> None:
        self.session.add(projection)
        await self.session.flush()

    async def get_projection(
        self,
        invocation_id: str,
        *,
        for_update: bool = False,
    ) -> ModelInvocationProjectionEntity | None:
        statement = select(ModelInvocationProjectionEntity).where(
            ModelInvocationProjectionEntity.invocation_id == invocation_id
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_projection_by_logical_attempt(
        self,
        logical_call_id: str,
        attempt_no: int,
        *,
        for_update: bool = False,
    ) -> ModelInvocationProjectionEntity | None:
        statement = select(ModelInvocationProjectionEntity).where(
            ModelInvocationProjectionEntity.logical_call_id == logical_call_id,
            ModelInvocationProjectionEntity.attempt_no == attempt_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list_invocation_events(
        self,
        invocation_id: str,
    ) -> list[ModelInvocationEventEntity]:
        statement = (
            select(ModelInvocationEventEntity)
            .where(ModelInvocationEventEntity.invocation_id == invocation_id)
            .order_by(ModelInvocationEventEntity.server_sequence.asc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def list_all_events(self) -> list[ModelInvocationEventEntity]:
        statement = select(ModelInvocationEventEntity).order_by(
            ModelInvocationEventEntity.server_sequence.asc()
        )
        return list((await self.session.execute(statement)).scalars().all())
    async def get_invocation_event(
        self,
        invocation_id: str,
        event_types: Sequence[str],
    ) -> ModelInvocationEventEntity | None:
        statement = (
            select(ModelInvocationEventEntity)
            .where(
                ModelInvocationEventEntity.invocation_id == invocation_id,
                ModelInvocationEventEntity.event_type.in_(tuple(event_types)),
            )
            .order_by(ModelInvocationEventEntity.server_sequence.asc())
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list_event_batch(
        self,
        *,
        after_sequence: int,
        through_sequence: int,
        limit: int,
    ) -> list[ModelInvocationEventEntity]:
        statement = (
            select(ModelInvocationEventEntity)
            .where(
                ModelInvocationEventEntity.server_sequence > after_sequence,
                ModelInvocationEventEntity.server_sequence <= through_sequence,
            )
            .order_by(ModelInvocationEventEntity.server_sequence.asc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def max_event_sequence(self) -> int:
        value = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(server_sequence), 0) "
                    "FROM tuge_model_invocation_event"
                )
            )
        ).scalar_one()
        return int(value)

    async def acquire_projection_rebuild_lock(self) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(1827003)")
            )
            await self.session.execute(
                text(
                    "LOCK TABLE tuge_model_invocation_event, "
                    "tuge_model_invocation_projection IN SHARE ROW EXCLUSIVE MODE"
                )
            )
        elif dialect == "sqlite":
            await self.session.execute(text("BEGIN IMMEDIATE"))
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported model observability dialect: {dialect}")

    async def create_projection_shadow(self, table_name: str) -> None:
        _validate_shadow_table_name(table_name)
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = (
                f"CREATE TEMP TABLE {table_name} "
                "(LIKE tuge_model_invocation_projection INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
        else:
            statement = (
                f"CREATE TEMP TABLE {table_name} AS "
                "SELECT * FROM tuge_model_invocation_projection WHERE 1 = 0"
            )
        await self.session.execute(text(statement))

    async def list_started_event_batch(
        self,
        *,
        after_sequence: int,
        limit: int,
    ) -> list[ModelInvocationEventEntity]:
        statement = (
            select(ModelInvocationEventEntity)
            .where(
                ModelInvocationEventEntity.event_type
                == "MODEL_INVOCATION_STARTED",
                ModelInvocationEventEntity.server_sequence > after_sequence,
            )
            .order_by(ModelInvocationEventEntity.server_sequence.asc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def list_events_for_invocations(
        self,
        invocation_ids: Sequence[str],
    ) -> list[ModelInvocationEventEntity]:
        if not invocation_ids:
            return []
        statement = (
            select(ModelInvocationEventEntity)
            .where(ModelInvocationEventEntity.invocation_id.in_(tuple(invocation_ids)))
            .order_by(ModelInvocationEventEntity.server_sequence.asc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def insert_projection_shadow(
        self,
        table_name: str,
        projections: Sequence[ModelInvocationProjectionEntity],
    ) -> None:
        _validate_shadow_table_name(table_name)
        if not projections:
            return
        columns = tuple(
            column.name for column in ModelInvocationProjectionEntity.__table__.columns
        )
        statement = text(
            f"INSERT INTO {table_name} ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + column for column in columns)})"
        )
        rows = [
            {column: getattr(projection, column) for column in columns}
            for projection in projections
        ]
        await self.session.execute(statement, rows)

    async def swap_projection_shadow(self, table_name: str) -> None:
        _validate_shadow_table_name(table_name)
        columns = tuple(
            column.name for column in ModelInvocationProjectionEntity.__table__.columns
        )
        mutable_columns = tuple(
            column for column in columns if column != "invocation_id"
        )
        dialect = self.session.get_bind().dialect.name
        select_suffix = "" if dialect == "postgresql" else " WHERE 1 = 1"
        update_clause = ", ".join(
            f"{column} = excluded.{column}" for column in mutable_columns
        )
        await self.session.execute(
            text(
                "INSERT INTO tuge_model_invocation_projection "
                f"({', '.join(columns)}) SELECT {', '.join(columns)} "
                f"FROM {table_name}{select_suffix} "
                "ON CONFLICT (invocation_id) DO UPDATE SET "
                f"{update_clause}"
            )
        )
        await self.session.execute(
            text(
                "DELETE FROM tuge_model_invocation_projection AS live "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table_name} AS shadow "
                "WHERE shadow.invocation_id = live.invocation_id)"
            )
        )
        await self.session.flush()
    async def delete_all_projections(self) -> None:
        await self.session.execute(delete(ModelInvocationProjectionEntity))
        await self.session.flush()

    async def list_running_before(
        self,
        cutoff: datetime,
        *,
        limit: int,
    ) -> list[ModelInvocationProjectionEntity]:
        statement = (
            select(ModelInvocationProjectionEntity)
            .where(
                ModelInvocationProjectionEntity.lifecycle_status == "RUNNING",
                ModelInvocationProjectionEntity.started_at <= cutoff,
            )
            .order_by(
                ModelInvocationProjectionEntity.started_at.asc(),
                ModelInvocationProjectionEntity.invocation_id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def query_projections(
        self,
        *,
        tenant_ids: Sequence[str],
        from_time: datetime,
        to_time: datetime,
        snapshot_to: datetime,
        limit: int,
        all_tenants: bool = False,
        task_id: str | None = None,
        run_id: str | None = None,
        stage_id: str | None = None,
        feature_code: str | None = None,
        logical_call_id: str | None = None,
        invocation_id: str | None = None,
        provider: str | None = None,
        model_pack_id: str | None = None,
        model_name: str | None = None,
        privacy_mode: str | None = None,
        route_type: str | None = None,
        lifecycle_status: str | None = None,
        dispatch_status: str | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
    ) -> list[ModelInvocationProjectionEntity]:
        _validate_tenant_scope(tenant_ids, all_tenants)
        conditions = [
            ModelInvocationProjectionEntity.started_at >= from_time,
            ModelInvocationProjectionEntity.started_at <= to_time,
            ModelInvocationProjectionEntity.ingested_at <= snapshot_to,
        ]
        if not all_tenants:
            conditions.append(
                ModelInvocationProjectionEntity.tenant_id.in_(tuple(tenant_ids))
            )
        optional = {
            ModelInvocationProjectionEntity.task_id: task_id,
            ModelInvocationProjectionEntity.run_id: run_id,
            ModelInvocationProjectionEntity.stage_id: stage_id,
            ModelInvocationProjectionEntity.feature_code: feature_code,
            ModelInvocationProjectionEntity.logical_call_id: logical_call_id,
            ModelInvocationProjectionEntity.invocation_id: invocation_id,
            ModelInvocationProjectionEntity.provider: provider,
            ModelInvocationProjectionEntity.model_pack_id: model_pack_id,
            ModelInvocationProjectionEntity.model_name: model_name,
            ModelInvocationProjectionEntity.privacy_mode: privacy_mode,
            ModelInvocationProjectionEntity.route_type: route_type,
            ModelInvocationProjectionEntity.lifecycle_status: lifecycle_status,
            ModelInvocationProjectionEntity.dispatch_status: dispatch_status,
            ModelInvocationProjectionEntity.outcome: outcome,
            ModelInvocationProjectionEntity.error_code: error_code,
        }
        conditions.extend(column == value for column, value in optional.items() if value is not None)
        statement = (
            select(ModelInvocationProjectionEntity)
            .where(*conditions)
            .order_by(
                ModelInvocationProjectionEntity.started_at.desc(),
                ModelInvocationProjectionEntity.invocation_id.desc(),
            )
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())
    async def aggregate_summary(
        self,
        *,
        tenant_ids: Sequence[str],
        from_time: datetime,
        to_time: datetime,
        all_tenants: bool = False,
        feature_code: str | None = None,
        provider: str | None = None,
        model_pack_id: str | None = None,
        model_name: str | None = None,
        privacy_mode: str | None = None,
        route_type: str | None = None,
    ) -> dict[str, object]:
        """Aggregate attempts and first-attempt logical cohorts in SQL."""

        projection = ModelInvocationProjectionEntity
        _validate_tenant_scope(tenant_ids, all_tenants)
        base_conditions = []
        if not all_tenants:
            base_conditions.append(projection.tenant_id.in_(tuple(tenant_ids)))
        optional = {
            projection.feature_code: feature_code,
            projection.provider: provider,
            projection.model_pack_id: model_pack_id,
            projection.model_name: model_name,
            projection.privacy_mode: privacy_mode,
            projection.route_type: route_type,
        }
        base_conditions.extend(
            column == value
            for column, value in optional.items()
            if value is not None
        )
        attempt_conditions = [
            *base_conditions,
            projection.started_at >= from_time,
            projection.started_at <= to_time,
        ]
        dispatched = projection.dispatch_status == "DISPATCHED"
        terminal = projection.lifecycle_status == "TERMINAL"
        success = dispatched & terminal & (projection.outcome == "SUCCESS")
        retry_eligible = projection.dispatch_status.in_(
            ("DISPATCHED", "DISPATCH_UNKNOWN")
        )

        main_row = (
            await self.session.execute(
                select(
                    func.count().label("dispatch_attempt_count"),
                    func.sum(case((dispatched, 1), else_=0)).label(
                        "dispatched_request_count"
                    ),
                    func.sum(
                        case(
                            (
                                terminal
                                & (projection.dispatch_status == "NOT_DISPATCHED"),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("not_dispatched_count"),
                    func.sum(
                        case(
                            (projection.dispatch_status == "DISPATCH_UNKNOWN", 1),
                            else_=0,
                        )
                    ).label("dispatch_unknown_count"),
                    func.sum(
                        case(
                            (projection.lifecycle_status == "RUNNING", 1),
                            else_=0,
                        )
                    ).label("active_attempt_count"),
                    func.sum(case((dispatched & terminal, 1), else_=0)).label(
                        "attempt_success_denominator"
                    ),
                    func.sum(case((success, 1), else_=0)).label(
                        "attempt_success_count"
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    dispatched,
                                    func.coalesce(
                                        projection.input_token_count,
                                        0,
                                    ),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("input_tokens"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    dispatched,
                                    func.coalesce(
                                        projection.output_token_count,
                                        0,
                                    ),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("output_tokens"),
                ).where(*attempt_conditions)
            )
        ).mappings().one()

        first_started = func.min(projection.started_at)
        first_attempts = (
            select(
                projection.logical_call_id.label("logical_call_id"),
                first_started.label("first_started_at"),
            )
            .where(*base_conditions)
            .group_by(projection.logical_call_id)
            .having(
                first_started >= from_time,
                first_started <= to_time,
            )
            .subquery()
        )
        cohort_conditions = [
            *base_conditions,
            projection.logical_call_id.in_(
                select(first_attempts.c.logical_call_id)
            ),
            projection.started_at <= to_time,
        ]
        logical_groups = (
            select(
                projection.logical_call_id.label("logical_call_id"),
                func.max(case((success, 1), else_=0)).label("logical_success"),
                func.sum(case((retry_eligible, 1), else_=0)).label(
                    "retry_eligible_count"
                ),
                func.sum(
                    case(
                        (projection.lifecycle_status == "RUNNING", 1),
                        else_=0,
                    )
                ).label("active_attempt_count"),
                func.sum(case((terminal, 1), else_=0)).label(
                    "terminal_attempt_count"
                ),
            )
            .where(*cohort_conditions)
            .group_by(projection.logical_call_id)
            .subquery()
        )
        completed_logical = (
            (logical_groups.c.active_attempt_count == 0)
            & (logical_groups.c.terminal_attempt_count > 0)
        )
        logical_row = (
            await self.session.execute(
                select(
                    func.count().label("logical_call_count"),
                    func.coalesce(
                        func.sum(
                            case((completed_logical, 1), else_=0)
                        ),
                        0,
                    ).label("logical_terminal_count"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    completed_logical
                                    & (logical_groups.c.logical_success == 1),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("logical_success_count"),
                    func.coalesce(
                        func.sum(
                            case(
                                (logical_groups.c.retry_eligible_count > 0, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("retry_denominator"),
                    func.coalesce(
                        func.sum(
                            case(
                                (logical_groups.c.retry_eligible_count > 1, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("retried_count"),
                )
            )
        ).mappings().one()

        cost_rows = list(
            (
                await self.session.execute(
                    select(
                        projection.cost_currency.label("currency"),
                        func.sum(projection.cost_amount).label("amount"),
                    )
                    .where(
                        *attempt_conditions,
                        dispatched,
                        projection.cost_amount.is_not(None),
                        projection.cost_currency.is_not(None),
                    )
                    .group_by(projection.cost_currency)
                    .order_by(projection.cost_currency.asc())
                )
            ).mappings()
        )

        dialect = self.session.get_bind().dialect.name
        latency_percentiles = await self._aggregate_percentiles(
            projection.latency_ms,
            conditions=attempt_conditions,
            dispatched=dispatched,
            dialect=dialect,
        )
        ttft_percentiles = await self._aggregate_percentiles(
            projection.time_to_first_token_ms,
            conditions=attempt_conditions,
            dispatched=dispatched,
            dialect=dialect,
        )
        time_series = await self._aggregate_time_series(
            base_conditions=base_conditions,
            first_attempts=first_attempts,
            through_time=to_time,
            dialect=dialect,
        )

        breakdowns: dict[str, list[dict[str, object]]] = {}
        for name, column, extra_conditions in (
            (
                "error",
                projection.error_code,
                (terminal, projection.error_code.is_not(None)),
            ),
            ("provider", projection.provider, (dispatched,)),
            ("model", projection.model_name, (dispatched,)),
            ("route_type", projection.route_type, ()),
            ("privacy_mode", projection.privacy_mode, ()),
        ):
            rows = (
                await self.session.execute(
                    select(
                        column.label("key"),
                        func.count().label("count"),
                    )
                    .where(*attempt_conditions, *extra_conditions)
                    .group_by(column)
                    .order_by(func.count().desc(), column.asc())
                )
            ).mappings()
            breakdowns[name] = [dict(row) for row in rows]

        return {
            "main": dict(main_row),
            "logical": dict(logical_row),
            "costs": [dict(row) for row in cost_rows],
            "latency_percentiles": latency_percentiles,
            "ttft_percentiles": ttft_percentiles,
            "time_series": time_series,
            "breakdowns": breakdowns,
        }

    async def _aggregate_percentiles(
        self,
        column,
        *,
        conditions: Sequence[object],
        dispatched,
        dialect: str,
    ) -> dict[str, float] | None:
        percentile_conditions = [
            *conditions,
            dispatched,
            column.is_not(None),
        ]
        if dialect == "postgresql":
            row = (
                await self.session.execute(
                    select(
                        func.percentile_cont(0.50)
                        .within_group(column)
                        .label("p50"),
                        func.percentile_cont(0.95)
                        .within_group(column)
                        .label("p95"),
                        func.percentile_cont(0.99)
                        .within_group(column)
                        .label("p99"),
                    ).where(*percentile_conditions)
                )
            ).mappings().one()
            return (
                {key: float(row[key]) for key in ("p50", "p95", "p99")}
                if row["p50"] is not None
                else None
            )
        values = [
            float(value)
            for value in (
                await self.session.execute(
                    select(column).where(*percentile_conditions)
                )
            ).scalars()
        ]
        return _linear_percentiles(values)

    async def _aggregate_time_series(
        self,
        *,
        base_conditions: Sequence[object],
        first_attempts,
        through_time: datetime,
        dialect: str,
    ) -> list[dict[str, object]]:
        projection = ModelInvocationProjectionEntity
        cohort_rows = (
            select(
                projection.logical_call_id.label("logical_call_id"),
                projection.lifecycle_status.label("lifecycle_status"),
                projection.dispatch_status.label("dispatch_status"),
                projection.outcome.label("outcome"),
                projection.latency_ms.label("latency_ms"),
                first_attempts.c.first_started_at.label("first_started_at"),
            )
            .join(
                first_attempts,
                first_attempts.c.logical_call_id
                == projection.logical_call_id,
            )
            .where(
                *base_conditions,
                projection.started_at <= through_time,
            )
            .subquery()
        )
        bucket = (
            func.date_trunc("hour", cohort_rows.c.first_started_at)
            if dialect == "postgresql"
            else func.strftime(
                "%Y-%m-%d %H:00:00",
                cohort_rows.c.first_started_at,
            )
        )
        dispatched = cohort_rows.c.dispatch_status == "DISPATCHED"
        terminal = cohort_rows.c.lifecycle_status == "TERMINAL"
        success = (
            dispatched
            & terminal
            & (cohort_rows.c.outcome == "SUCCESS")
        )
        columns = [
            bucket.label("bucket_start"),
            func.count(func.distinct(cohort_rows.c.logical_call_id)).label(
                "logical_call_count"
            ),
            func.count().label("dispatch_attempt_count"),
            func.sum(case((dispatched, 1), else_=0)).label(
                "dispatched_request_count"
            ),
        ]
        if dialect == "postgresql":
            columns.append(
                func.percentile_cont(0.95)
                .within_group(
                    case(
                        (dispatched, cohort_rows.c.latency_ms),
                        else_=None,
                    )
                )
                .label("p95_latency_ms")
            )
        rows = list(
            (
                await self.session.execute(
                    select(*columns)
                    .group_by(bucket)
                    .order_by(bucket.asc())
                )
            ).mappings()
        )
        logical_bucket_groups = (
            select(
                bucket.label("bucket_start"),
                cohort_rows.c.logical_call_id.label("logical_call_id"),
                func.max(case((success, 1), else_=0)).label(
                    "logical_success"
                ),
                func.sum(
                    case(
                        (cohort_rows.c.lifecycle_status == "RUNNING", 1),
                        else_=0,
                    )
                ).label("active_attempt_count"),
                func.sum(case((terminal, 1), else_=0)).label(
                    "terminal_attempt_count"
                ),
            )
            .group_by(bucket, cohort_rows.c.logical_call_id)
            .subquery()
        )
        completed_logical = (
            (logical_bucket_groups.c.active_attempt_count == 0)
            & (logical_bucket_groups.c.terminal_attempt_count > 0)
        )
        logical_rows = (
            await self.session.execute(
                select(
                    logical_bucket_groups.c.bucket_start,
                    func.sum(
                        case(
                            (
                                completed_logical
                                & (logical_bucket_groups.c.logical_success == 1),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("success_count"),
                    func.sum(
                        case((completed_logical, 1), else_=0)
                    ).label("success_denominator"),
                )
                .group_by(logical_bucket_groups.c.bucket_start)
            )
        ).mappings()
        logical_by_bucket = {
            row["bucket_start"]: dict(row)
            for row in logical_rows
        }
        sqlite_latencies: dict[object, list[float]] = {}
        if dialect != "postgresql":
            for bucket_value, latency in (
                await self.session.execute(
                    select(bucket, cohort_rows.c.latency_ms).where(
                        dispatched,
                        cohort_rows.c.latency_ms.is_not(None),
                    )
                )
            ):
                sqlite_latencies.setdefault(bucket_value, []).append(
                    float(latency)
                )
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            logical = logical_by_bucket.get(item["bucket_start"], {})
            item["success_count"] = int(logical.get("success_count") or 0)
            item["success_denominator"] = int(
                logical.get("success_denominator") or 0
            )
            if dialect != "postgresql":
                percentiles = _linear_percentiles(
                    sqlite_latencies.get(item["bucket_start"], [])
                )
                item["p95_latency_ms"] = (
                    percentiles["p95"] if percentiles is not None else 0.0
                )
            elif item["p95_latency_ms"] is None:
                item["p95_latency_ms"] = 0.0
            result.append(item)
        return result

    async def get_event(
        self, event_id: str
    ) -> ModelInvocationEventEntity | None:
        statement = select(ModelInvocationEventEntity).where(
            ModelInvocationEventEntity.event_id == event_id
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def query_events(
        self,
        *,
        tenant_ids: Sequence[str],
        from_time: datetime,
        to_time: datetime,
        snapshot_to: datetime,
        limit: int,
        all_tenants: bool = False,
        task_id: str | None = None,
        run_id: str | None = None,
        logical_call_id: str | None = None,
        invocation_id: str | None = None,
        event_id: str | None = None,
        event_type: str | None = None,
    ) -> list[ModelInvocationEventEntity]:
        _validate_tenant_scope(tenant_ids, all_tenants)
        conditions = [
            ModelInvocationEventEntity.occurred_at >= from_time,
            ModelInvocationEventEntity.occurred_at <= to_time,
            ModelInvocationEventEntity.ingested_at <= snapshot_to,
        ]
        if not all_tenants:
            conditions.append(
                ModelInvocationEventEntity.tenant_id.in_(tuple(tenant_ids))
            )
        optional = {
            ModelInvocationEventEntity.task_id: task_id,
            ModelInvocationEventEntity.run_id: run_id,
            ModelInvocationEventEntity.logical_call_id: logical_call_id,
            ModelInvocationEventEntity.invocation_id: invocation_id,
            ModelInvocationEventEntity.event_id: event_id,
            ModelInvocationEventEntity.event_type: event_type,
        }
        conditions.extend(column == value for column, value in optional.items() if value is not None)
        statement = (
            select(ModelInvocationEventEntity)
            .where(*conditions)
            .order_by(
                ModelInvocationEventEntity.occurred_at.desc(),
                ModelInvocationEventEntity.event_id.desc(),
            )
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())


def _validate_tenant_scope(tenant_ids: Sequence[str], all_tenants: bool) -> None:
    normalized = tuple(tenant_ids)
    if all_tenants:
        if normalized:
            raise ValueError("all-tenant query cannot carry tenant ids")
        return
    if not normalized or any(
        not isinstance(item, str) or not item for item in normalized
    ):
        raise ValueError("tenant-scoped query requires tenant ids")


def _validate_shadow_table_name(value: str) -> None:
    prefix = "tuge_model_projection_shadow_"
    suffix = value.removeprefix(prefix)
    if not value.startswith(prefix) or not suffix or not suffix.isalnum():
        raise ValueError("invalid projection shadow table name")
def _linear_percentiles(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(quantile: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * quantile
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    return {
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }
