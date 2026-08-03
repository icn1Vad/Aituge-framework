from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import timedelta

import psycopg
import model_observability.lifecycle as lifecycle_module
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_observability.domain import (
    InvocationMetrics,
    ModelSnapshotCapacityError,
    InvocationOutcome,
    InvalidInvocationTransitionError,
    ModelInvocationContext,
    ModelInvocationEventType,
    PrivacyMode,
    RouteType,
    utc_now,
)
from model_observability.lifecycle import ModelInvocationLifecycleService
from model_observability.migrate import (
    ModelObservabilityMigrationError,
    discover_migrations,
    run_migrations,
)
from model_observability.repository import ModelInvocationRepository
from model_observability.snapshot import ModelSnapshotStore, stable_query_hash
from model_observability.query import (
    ModelEventFilters,
    ModelInvocationFilters,
    ModelInvocationQueryService,
    ModelQueryScope,
    ModelSummaryFilters,
)


_TEST_DATABASE_URL = os.getenv("MODEL_OBSERVABILITY_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _TEST_DATABASE_URL,
    reason="requires an explicit disposable PostgreSQL database",
)


@pytest_asyncio.fixture
async def postgres_session_factory():
    schema_name = f"obs20_{uuid.uuid4().hex}"
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(schema_name)
            )
        )
        for migration in discover_migrations():
            connection.execute(migration.up_path.read_text("utf-8"))

    async_url = _TEST_DATABASE_URL.replace(
        "postgresql://",
        "postgresql+asyncpg://",
        1,
    )
    engine = create_async_engine(
        async_url,
        pool_size=25,
        max_overflow=5,
        connect_args={
            "server_settings": {
                "search_path": schema_name,
                "timezone": "UTC",
            }
        },
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        session = maker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    session_context.schema_name = schema_name
    yield session_context
    await engine.dispose()
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(
                sql.Identifier(schema_name)
            )
        )

def _context(
    suffix: str,
    *,
    logical_call_id: str | None = None,
    attempt_no: int = 1,
    fallback_from_invocation_id: str | None = None,
) -> ModelInvocationContext:
    return ModelInvocationContext(
        tenant_id="tenant-pg-integration",
        feature_code="contract.review",
        provider="provider-pg",
        model_name="model-pg",
        privacy_mode=PrivacyMode.STANDARD,
        route_type=RouteType.EXTERNAL,
        logical_call_id=logical_call_id or f"logical-{suffix}",
        invocation_id=f"inv-{suffix}",
        attempt_no=attempt_no,
        fallback_from_invocation_id=fallback_from_invocation_id,
    )


@pytest.mark.asyncio
async def test_postgres_serializes_idempotency_and_uses_server_causality(
    postgres_session_factory,
) -> None:
    service = ModelInvocationLifecycleService(postgres_session_factory)
    suffix = uuid.uuid4().hex
    context = _context(suffix)
    started_at = utc_now()

    rows = await asyncio.gather(
        *[
            service.start_invocation(context, occurred_at=started_at)
            for _ in range(20)
        ]
    )
    assert {row.invocation_id for row in rows} == {context.invocation_id}

    await asyncio.gather(
        *[
            service.mark_dispatched(
                context.invocation_id,
                provider_request_id="provider-request-stable",
                occurred_at=started_at + timedelta(seconds=1),
            )
            for _ in range(20)
        ]
    )
    metrics = InvocationMetrics(input_tokens=7, output_tokens=3, latency_ms=15)
    await asyncio.gather(
        *[
            service.terminate(
                context.invocation_id,
                event_type=ModelInvocationEventType.SUCCEEDED,
                outcome=InvocationOutcome.SUCCESS,
                metrics=metrics,
                occurred_at=started_at + timedelta(seconds=2),
            )
            for _ in range(20)
        ]
    )

    events = await service.get_events(context.invocation_id)
    assert len(events) == 3
    assert [event.server_sequence for event in events] == sorted(
        event.server_sequence for event in events
    )
    projection = await service.get_projection(context.invocation_id)
    assert projection.finished_at > projection.started_at
    assert projection.started_sequence < projection.dispatch_sequence
    assert projection.dispatch_sequence < projection.terminal_sequence

    async with postgres_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tuge_model_invocation_projection "
                "SET model_name = 'corrupted-rebuild-source' "
                "WHERE invocation_id = :invocation_id"
            ),
            {"invocation_id": context.invocation_id},
        )
    assert await service.rebuild_projection(batch_size=1) >= 1
    assert (
        await service.get_projection(context.invocation_id)
    ).model_name == "model-pg"


@pytest.mark.asyncio
async def test_postgres_snapshot_is_shared_across_twenty_workers_and_summary_is_sql(
    postgres_session_factory,
) -> None:
    lifecycle = ModelInvocationLifecycleService(postgres_session_factory)
    suffix = uuid.uuid4().hex
    for index in range(3):
        invocation = await lifecycle.start_invocation(
            _context(f"{suffix}-{index}")
        )
        if index == 0:
            await lifecycle.mark_dispatched(invocation.invocation_id)

    scope = ModelQueryScope(
        ("tenant-pg-integration",),
        f"worker-shared-scope-{suffix}",
    )
    now = utc_now()
    from_time = now - timedelta(hours=1)
    to_time = now + timedelta(minutes=1)
    services = [
        ModelInvocationQueryService(postgres_session_factory)
        for _ in range(20)
    ]

    async def first_page(service: ModelInvocationQueryService):
        return await service.list_invocations(
            scope=scope,
            from_time=from_time,
            to_time=to_time,
            snapshot_to=to_time,
            filters=ModelInvocationFilters(),
            high_watermark=None,
            cursor=None,
            limit=1,
        )

    pages = await asyncio.gather(*(first_page(service) for service in services))
    handles = {
        page.watermark.high_watermark_handle
        for page in pages
    }
    assert len(handles) == 1
    assert all(page.page.has_more for page in pages)

    first = pages[0]
    second = await services[-1].list_invocations(
        scope=scope,
        from_time=from_time,
        to_time=to_time,
        snapshot_to=to_time,
        filters=ModelInvocationFilters(),
        high_watermark=first.watermark.high_watermark_handle,
        cursor=first.page.next_cursor,
        limit=1,
    )
    assert second.data
    assert second.data[0].invocation_id != first.data[0].invocation_id

    summary = await services[0].summary(
        scope=scope,
        from_time=from_time,
        to_time=to_time,
        filters=ModelSummaryFilters(),
    )
    assert summary.dispatch_attempt_count >= 3
    assert summary.provider_breakdown



def _sync_connection_for_schema(schema_name: str):
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    connection = psycopg.connect(sync_url, autocommit=True)
    connection.execute(
        sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
    )
    return connection


@pytest.mark.asyncio
async def test_postgres_rejects_invalid_payload_hash_and_terminal_dispatch(
    postgres_session_factory,
) -> None:
    schema_name = postgres_session_factory.schema_name
    bad_started_sql = """
        INSERT INTO tuge_model_invocation_event (
          event_id,
          server_sequence,
          event_type,
          schema_version,
          logical_call_id,
          invocation_id,
          attempt_no,
          dispatch_status,
          occurred_at,
          ingested_at,
          tenant_id,
          feature_code,
          provider,
          provider_request_id_hash,
          model_name,
          privacy_mode,
          route_type,
          input_token_count,
          metadata_json
        )
        VALUES (
          %(event_id)s,
          (
            SELECT COALESCE(MAX(server_sequence), 0) + 1
            FROM tuge_model_invocation_event
          ),
          'MODEL_INVOCATION_STARTED',
          1,
          %(logical_call_id)s,
          %(invocation_id)s,
          1,
          'NOT_DISPATCHED',
          now(),
          now(),
          'tenant-pg-integration',
          'contract.review',
          'provider-pg',
          %(provider_hash)s,
          'model-pg',
          'STANDARD',
          'EXTERNAL',
          %(input_tokens)s,
          %(metadata)s
        )
    """
    invalid_rows = [
        {
            "event_id": "bad-metadata",
            "logical_call_id": "logical-bad-metadata",
            "invocation_id": "inv-bad-metadata",
            "provider_hash": None,
            "input_tokens": None,
            "metadata": Jsonb([]),
        },
        {
            "event_id": "bad-hash",
            "logical_call_id": "logical-bad-hash",
            "invocation_id": "inv-bad-hash",
            "provider_hash": "z" * 64,
            "input_tokens": None,
            "metadata": Jsonb({}),
        },
        {
            "event_id": "bad-started-payload",
            "logical_call_id": "logical-bad-started-payload",
            "invocation_id": "inv-bad-started-payload",
            "provider_hash": None,
            "input_tokens": 1,
            "metadata": Jsonb({}),
        },
    ]
    with _sync_connection_for_schema(schema_name) as connection:
        for row in invalid_rows:
            with pytest.raises(psycopg.errors.CheckViolation):
                connection.execute(bad_started_sql, row)

    lifecycle = ModelInvocationLifecycleService(postgres_session_factory)
    validation = await lifecycle.start_invocation(
        _context(f"validation-{uuid.uuid4().hex}")
    )
    bad_validation_sql = """
        INSERT INTO tuge_model_invocation_event (
          event_id,
          server_sequence,
          event_type,
          schema_version,
          logical_call_id,
          invocation_id,
          attempt_no,
          dispatch_status,
          fallback_from_invocation_id,
          occurred_at,
          ingested_at,
          tenant_id,
          feature_code,
          provider,
          model_name,
          privacy_mode,
          route_type,
          outcome,
          error_code,
          metadata_json
        )
        SELECT
          %(event_id)s,
          (
            SELECT COALESCE(MAX(server_sequence), 0) + 1
            FROM tuge_model_invocation_event
          ),
          'MODEL_INVOCATION_VALIDATION_FAILED',
          schema_version,
          logical_call_id,
          invocation_id,
          attempt_no,
          'NOT_DISPATCHED',
          fallback_from_invocation_id,
          now(),
          now(),
          tenant_id,
          feature_code,
          provider,
          model_name,
          privacy_mode,
          route_type,
          'FAILURE',
          'MODEL_OUTPUT_SCHEMA_INVALID',
          '{}'::jsonb
        FROM tuge_model_invocation_event
        WHERE invocation_id = %(invocation_id)s
          AND event_type = 'MODEL_INVOCATION_STARTED'
    """
    with _sync_connection_for_schema(schema_name) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                bad_validation_sql,
                {
                    "event_id": f"bad-validation-{uuid.uuid4().hex}",
                    "invocation_id": validation.invocation_id,
                },
            )

    tampered = await lifecycle.start_invocation(
        _context(f"hash-tamper-{uuid.uuid4().hex}")
    )
    await lifecycle.mark_dispatched(
        tampered.invocation_id,
        provider_request_id="provider-request-original",
    )
    bad_hash_sql = """
        INSERT INTO tuge_model_invocation_event (
          event_id,
          server_sequence,
          event_type,
          schema_version,
          logical_call_id,
          invocation_id,
          attempt_no,
          dispatch_status,
          fallback_from_invocation_id,
          occurred_at,
          ingested_at,
          tenant_id,
          feature_code,
          provider,
          provider_request_id_hash,
          model_name,
          privacy_mode,
          route_type,
          outcome,
          error_code,
          metadata_json
        )
        SELECT
          %(event_id)s,
          (
            SELECT COALESCE(MAX(server_sequence), 0) + 1
            FROM tuge_model_invocation_event
          ),
          'MODEL_INVOCATION_FAILED',
          schema_version,
          logical_call_id,
          invocation_id,
          attempt_no,
          'DISPATCHED',
          fallback_from_invocation_id,
          now(),
          now(),
          tenant_id,
          feature_code,
          provider,
          %(provider_hash)s,
          model_name,
          privacy_mode,
          route_type,
          'FAILURE',
          'MODEL_PROVIDER_HTTP_ERROR',
          '{}'::jsonb
        FROM tuge_model_invocation_event
        WHERE invocation_id = %(invocation_id)s
          AND event_type = 'MODEL_INVOCATION_STARTED'
    """
    with _sync_connection_for_schema(schema_name) as connection:
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="MODEL_INVOCATION_DISPATCH_FACT_INVALID",
        ):
            connection.execute(
                bad_hash_sql,
                {
                    "event_id": f"bad-hash-terminal-{uuid.uuid4().hex}",
                    "invocation_id": tampered.invocation_id,
                    "provider_hash": "0" * 64,
                },
            )


def test_postgres_002_preflight_rejects_bad_001_data_atomically() -> None:
    schema_name = f"obs20_bad_{uuid.uuid4().hex}"
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    migrations = discover_migrations()
    with psycopg.connect(sync_url, autocommit=True) as connection:
        try:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.execute(migrations[0].up_path.read_text("utf-8"))
            connection.execute(
                """
                INSERT INTO tuge_model_invocation_event (
                  event_id,
                  event_type,
                  schema_version,
                  logical_call_id,
                  invocation_id,
                  attempt_no,
                  dispatch_status,
                  occurred_at,
                  tenant_id,
                  feature_code,
                  provider,
                  model_name,
                  privacy_mode,
                  route_type,
                  metadata_json
                )
                VALUES (
                  'bad-started',
                  'MODEL_INVOCATION_STARTED',
                  1,
                  'logical-bad',
                  'inv-bad',
                  1,
                  'NOT_DISPATCHED',
                  now(),
                  'tenant-bad',
                  'contract.review',
                  'provider-bad',
                  'model-bad',
                  'STANDARD',
                  'EXTERNAL',
                  '[]'::jsonb
                )
                """
            )
            connection.execute(
                """
                INSERT INTO tuge_model_invocation_projection (
                  invocation_id,
                  projection_version,
                  data_as_of,
                  logical_call_id,
                  attempt_no,
                  tenant_id,
                  feature_code,
                  started_at,
                  ingested_at,
                  lifecycle_status,
                  dispatch_status,
                  provider,
                  model_name,
                  privacy_mode,
                  route_type
                )
                VALUES (
                  'inv-bad',
                  1,
                  now(),
                  'logical-bad',
                  1,
                  'tenant-bad',
                  'contract.review',
                  now(),
                  now(),
                  'RUNNING',
                  'NOT_DISPATCHED',
                  'provider-bad',
                  'model-bad',
                  'STANDARD',
                  'EXTERNAL'
                )
                """
            )

            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="MODEL_OBSERVABILITY_PREFLIGHT_VALUE_INVALID",
            ):
                connection.execute(migrations[1].up_path.read_text("utf-8"))

            assert connection.execute(
                "SELECT to_regclass('tuge_model_observability_sequence')"
            ).fetchone()[0] is None
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'tuge_model_invocation_event'
                  AND column_name = 'server_sequence'
                """
            ).fetchone()[0] == 0
        finally:
            connection.execute("SET search_path TO public")
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


def test_postgres_002_up_down_up_round_trip() -> None:
    schema_name = f"obs20_roundtrip_{uuid.uuid4().hex}"
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    migrations = discover_migrations()
    with psycopg.connect(sync_url, autocommit=True) as connection:
        try:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.execute(migrations[0].up_path.read_text("utf-8"))
            connection.execute(
                """
                INSERT INTO tuge_model_invocation_event (
                  event_id, event_type, logical_call_id, invocation_id,
                  attempt_no, dispatch_status, fallback_from_invocation_id,
                  occurred_at, ingested_at, tenant_id, feature_code,
                  provider, provider_request_id_hash, model_name,
                  privacy_mode, route_type, input_token_count,
                  output_token_count, latency_ms, time_to_first_token_ms,
                  outcome, error_code, retry_reason, metadata_json
                )
                VALUES
                  (
                    'event-a-started', 'MODEL_INVOCATION_STARTED',
                    'logical-roundtrip', 'inv-roundtrip-a', 1,
                    'NOT_DISPATCHED', NULL,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                    'tenant-roundtrip', 'contract.review', 'provider-a',
                    NULL, 'model-a', 'STANDARD', 'EXTERNAL',
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{}'::jsonb
                  ),
                  (
                    'event-a-dispatched', 'MODEL_INVOCATION_DISPATCHED',
                    'logical-roundtrip', 'inv-roundtrip-a', 1,
                    'DISPATCHED', NULL,
                    '2026-01-01T00:00:01Z', '2026-01-01T00:00:01Z',
                    'tenant-roundtrip', 'contract.review', 'provider-a',
                    repeat('a', 64), 'model-a', 'STANDARD', 'EXTERNAL',
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{}'::jsonb
                  ),
                  (
                    'event-a-terminal', 'MODEL_INVOCATION_FAILED',
                    'logical-roundtrip', 'inv-roundtrip-a', 1,
                    'DISPATCHED', NULL,
                    '2026-01-01T00:00:02Z', '2026-01-01T00:00:02Z',
                    'tenant-roundtrip', 'contract.review', 'provider-a',
                    repeat('a', 64), 'model-a', 'STANDARD', 'EXTERNAL',
                    11, 0, 25, 10, 'FAILURE',
                    'MODEL_PROVIDER_HTTP_ERROR', 'PROVIDER_RETRY', '{}'::jsonb
                  ),
                  (
                    'event-b-started', 'MODEL_INVOCATION_STARTED',
                    'logical-roundtrip', 'inv-roundtrip-b', 2,
                    'NOT_DISPATCHED', 'inv-roundtrip-a',
                    '2026-01-01T00:00:03Z', '2026-01-01T00:00:03Z',
                    'tenant-roundtrip', 'contract.review', 'provider-b',
                    NULL, 'model-b', 'STANDARD', 'EXTERNAL',
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{}'::jsonb
                  ),
                  (
                    'event-b-dispatched', 'MODEL_INVOCATION_DISPATCHED',
                    'logical-roundtrip', 'inv-roundtrip-b', 2,
                    'DISPATCHED', 'inv-roundtrip-a',
                    '2026-01-01T00:00:04Z', '2026-01-01T00:00:04Z',
                    'tenant-roundtrip', 'contract.review', 'provider-b',
                    repeat('b', 64), 'model-b', 'STANDARD', 'EXTERNAL',
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{}'::jsonb
                  ),
                  (
                    'event-b-terminal', 'MODEL_INVOCATION_SUCCEEDED',
                    'logical-roundtrip', 'inv-roundtrip-b', 2,
                    'DISPATCHED', 'inv-roundtrip-a',
                    '2026-01-01T00:00:05Z', '2026-01-01T00:00:05Z',
                    'tenant-roundtrip', 'contract.review', 'provider-b',
                    repeat('b', 64), 'model-b', 'STANDARD', 'EXTERNAL',
                    13, 5, 30, 12, 'SUCCESS', NULL, NULL, '{}'::jsonb
                  )
                """
            )
            connection.execute(
                """
                INSERT INTO tuge_model_invocation_projection (
                  invocation_id, projection_version, data_as_of,
                  logical_call_id, attempt_no, fallback_from_invocation_id,
                  tenant_id, feature_code, started_at, finished_at,
                  ingested_at, lifecycle_status, dispatch_status, outcome,
                  error_code, retry_reason, provider,
                  provider_request_id_hash, model_name, privacy_mode,
                  route_type, input_token_count, output_token_count,
                  latency_ms, time_to_first_token_ms
                )
                VALUES
                  (
                    'inv-roundtrip-a', 3, '2026-01-01T00:00:02Z',
                    'logical-roundtrip', 1, NULL,
                    'tenant-roundtrip', 'contract.review',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:02Z',
                    '2026-01-01T00:00:02Z', 'TERMINAL', 'DISPATCHED',
                    'FAILURE', 'MODEL_PROVIDER_HTTP_ERROR', 'PROVIDER_RETRY',
                    'provider-a', repeat('a', 64), 'model-a',
                    'STANDARD', 'EXTERNAL', 11, 0, 25, 10
                  ),
                  (
                    'inv-roundtrip-b', 3, '2026-01-01T00:00:05Z',
                    'logical-roundtrip', 2, 'inv-roundtrip-a',
                    'tenant-roundtrip', 'contract.review',
                    '2026-01-01T00:00:03Z', '2026-01-01T00:00:05Z',
                    '2026-01-01T00:00:05Z', 'TERMINAL', 'DISPATCHED',
                    'SUCCESS', NULL, NULL, 'provider-b', repeat('b', 64),
                    'model-b', 'STANDARD', 'EXTERNAL', 13, 5, 30, 12
                  )
                """
            )
            parity = connection.execute(
                """
                SELECT projection.invocation_id,
                       ROW(
                         projection.logical_call_id, projection.attempt_no,
                         projection.fallback_from_invocation_id,
                         projection.tenant_id, projection.user_id,
                         projection.feature_code, projection.task_id,
                         projection.run_id, projection.stage_id,
                         projection.request_id, projection.trace_id,
                         projection.provider, projection.model_pack_id,
                         projection.model_pack_version, projection.model_name,
                         projection.deployment_name, projection.provider_region,
                         projection.privacy_mode, projection.route_type,
                         projection.model_config_id, projection.service_version,
                         projection.started_at
                       ) IS DISTINCT FROM ROW(
                         started.logical_call_id, started.attempt_no,
                         started.fallback_from_invocation_id,
                         started.tenant_id, started.user_id,
                         started.feature_code, started.task_id,
                         started.run_id, started.stage_id,
                         started.request_id, started.trace_id,
                         started.provider, started.model_pack_id,
                         started.model_pack_version, started.model_name,
                         started.deployment_name, started.provider_region,
                         started.privacy_mode, started.route_type,
                         started.model_config_id, started.service_version,
                         started.occurred_at
                       ),
                       projection.lifecycle_status IS DISTINCT FROM 'TERMINAL',
                       projection.projection_version IS DISTINCT FROM 3,
                       projection.dispatch_status IS DISTINCT FROM
                         dispatch.dispatch_status,
                       projection.provider_request_id_hash IS DISTINCT FROM
                         terminal.provider_request_id_hash,
                       projection.ingested_at IS DISTINCT FROM terminal.ingested_at,
                       projection.data_as_of IS DISTINCT FROM terminal.ingested_at,
                       ROW(
                         projection.finished_at, projection.outcome,
                         projection.error_code, projection.retry_reason,
                         projection.input_token_count,
                         projection.output_token_count, projection.latency_ms,
                         projection.time_to_first_token_ms,
                         projection.cost_amount, projection.cost_currency,
                         projection.cost_source, projection.pricing_version,
                         projection.cost_calculated_at
                       ) IS DISTINCT FROM ROW(
                         terminal.occurred_at, terminal.outcome,
                         terminal.error_code, terminal.retry_reason,
                         terminal.input_token_count,
                         terminal.output_token_count, terminal.latency_ms,
                         terminal.time_to_first_token_ms,
                         terminal.cost_amount, terminal.cost_currency,
                         terminal.cost_source, terminal.pricing_version,
                         terminal.cost_calculated_at
                       )
                FROM tuge_model_invocation_projection AS projection
                JOIN tuge_model_invocation_event AS started
                  ON started.invocation_id = projection.invocation_id
                 AND started.event_type = 'MODEL_INVOCATION_STARTED'
                JOIN tuge_model_invocation_event AS dispatch
                  ON dispatch.invocation_id = projection.invocation_id
                 AND dispatch.event_type = 'MODEL_INVOCATION_DISPATCHED'
                JOIN tuge_model_invocation_event AS terminal
                  ON terminal.invocation_id = projection.invocation_id
                 AND terminal.event_type IN (
                   'MODEL_INVOCATION_SUCCEEDED',
                   'MODEL_INVOCATION_FAILED'
                 )
                ORDER BY projection.invocation_id
                """
            ).fetchall()
            assert parity == [
                (
                    "inv-roundtrip-a",
                    False, False, False, False, False, False, False, False,
                ),
                (
                    "inv-roundtrip-b",
                    False, False, False, False, False, False, False, False,
                ),
            ]
            connection.execute(migrations[1].up_path.read_text("utf-8"))

            event_fact_sql = """
                SELECT event_id, event_type, schema_version, logical_call_id,
                       invocation_id, attempt_no, dispatch_status,
                       fallback_from_invocation_id, occurred_at, ingested_at,
                       tenant_id, feature_code, provider,
                       provider_request_id_hash, model_name, privacy_mode,
                       route_type, input_token_count, output_token_count,
                       latency_ms, time_to_first_token_ms, outcome,
                       error_code, retry_reason, metadata_json
                FROM tuge_model_invocation_event
                WHERE invocation_id IN ('inv-roundtrip-a', 'inv-roundtrip-b')
                ORDER BY event_id
            """
            projection_fact_sql = """
                SELECT invocation_id, projection_version, data_as_of,
                       logical_call_id, attempt_no, fallback_from_invocation_id,
                       tenant_id, feature_code, started_at, finished_at,
                       ingested_at, lifecycle_status, dispatch_status, outcome,
                       error_code, retry_reason, provider,
                       provider_request_id_hash, model_name, privacy_mode,
                       route_type, input_token_count, output_token_count,
                       latency_ms, time_to_first_token_ms
                FROM tuge_model_invocation_projection
                WHERE invocation_id IN ('inv-roundtrip-a', 'inv-roundtrip-b')
                ORDER BY invocation_id
            """
            event_facts_before = connection.execute(event_fact_sql).fetchall()
            projection_facts_before = connection.execute(
                projection_fact_sql
            ).fetchall()
            assert len(event_facts_before) == 6
            assert len(projection_facts_before) == 2
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conrelid = 'tuge_model_invocation_event'::regclass
                  AND conname = 'ck_tuge_model_event_schema'
                """
            ).fetchone()[0] == 1

            connection.execute(migrations[1].down_path.read_text("utf-8"))
            assert connection.execute(
                "SELECT to_regclass('tuge_model_observability_sequence')"
            ).fetchone()[0] is None
            assert connection.execute(event_fact_sql).fetchall() == event_facts_before
            assert connection.execute(
                projection_fact_sql
            ).fetchall() == projection_facts_before
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conrelid = 'tuge_model_invocation_projection'::regclass
                  AND conname = 'ck_tuge_model_projection_time_order'
                """
            ).fetchone()[0] == 1
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conrelid = 'tuge_model_invocation_event'::regclass
                  AND conname = 'ck_tuge_model_event_schema'
                """
            ).fetchone()[0] == 0

            # A valid 001-era write while hardening is rolled back must migrate too.
            connection.execute(
                """
                INSERT INTO tuge_model_invocation_event (
                  event_id, event_type, logical_call_id, invocation_id,
                  attempt_no, dispatch_status, fallback_from_invocation_id,
                  occurred_at, ingested_at, tenant_id, feature_code,
                  provider, model_name, privacy_mode, route_type, metadata_json
                )
                VALUES (
                  'event-c-started', 'MODEL_INVOCATION_STARTED',
                  'logical-roundtrip', 'inv-roundtrip-c', 3,
                  'NOT_DISPATCHED', 'inv-roundtrip-b',
                  '2026-01-01T00:00:06Z', '2026-01-01T00:00:06Z',
                  'tenant-roundtrip', 'contract.review', 'provider-c',
                  'model-c', 'STANDARD', 'EXTERNAL', '{}'::jsonb
                )
                """
            )
            connection.execute(
                """
                INSERT INTO tuge_model_invocation_projection (
                  invocation_id, projection_version, data_as_of,
                  logical_call_id, attempt_no, fallback_from_invocation_id,
                  tenant_id, feature_code, started_at, ingested_at,
                  lifecycle_status, dispatch_status, provider, model_name,
                  privacy_mode, route_type
                )
                VALUES (
                  'inv-roundtrip-c', 1, '2026-01-01T00:00:06Z',
                  'logical-roundtrip', 3, 'inv-roundtrip-b',
                  'tenant-roundtrip', 'contract.review',
                  '2026-01-01T00:00:06Z', '2026-01-01T00:00:06Z',
                  'RUNNING', 'NOT_DISPATCHED', 'provider-c', 'model-c',
                  'STANDARD', 'EXTERNAL'
                )
                """
            )

            connection.execute(migrations[1].up_path.read_text("utf-8"))
            assert connection.execute(event_fact_sql).fetchall() == event_facts_before
            assert connection.execute(
                projection_fact_sql
            ).fetchall() == projection_facts_before
            assert connection.execute(
                "SELECT COUNT(*) FROM tuge_model_invocation_event"
            ).fetchone()[0] == 7
            assert connection.execute(
                "SELECT COUNT(*) FROM tuge_model_invocation_projection"
            ).fetchone()[0] == 3
            assert connection.execute(
                """
                SELECT COUNT(*) = COUNT(DISTINCT server_sequence)
                FROM tuge_model_invocation_event
                """
            ).fetchone()[0] is True
            assert connection.execute(
                """
                SELECT bool_and(
                  projection.started_sequence = started.server_sequence
                  AND projection.dispatch_sequence IS NOT DISTINCT FROM
                      dispatched.server_sequence
                  AND projection.terminal_sequence IS NOT DISTINCT FROM
                      terminal.server_sequence
                )
                FROM tuge_model_invocation_projection AS projection
                JOIN tuge_model_invocation_event AS started
                  ON started.invocation_id = projection.invocation_id
                 AND started.event_type = 'MODEL_INVOCATION_STARTED'
                LEFT JOIN tuge_model_invocation_event AS dispatched
                  ON dispatched.invocation_id = projection.invocation_id
                 AND dispatched.event_type IN (
                   'MODEL_INVOCATION_DISPATCHED',
                   'MODEL_INVOCATION_DISPATCH_UNKNOWN'
                 )
                LEFT JOIN tuge_model_invocation_event AS terminal
                  ON terminal.invocation_id = projection.invocation_id
                 AND terminal.event_type IN (
                   'MODEL_INVOCATION_SUCCEEDED',
                   'MODEL_INVOCATION_FAILED',
                   'MODEL_INVOCATION_VALIDATION_FAILED',
                   'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
                   'MODEL_INVOCATION_OUTCOME_UNKNOWN',
                   'MODEL_INVOCATION_ABANDONED'
                 )
                """
            ).fetchone()[0] is True
            assert connection.execute(
                """
                SELECT bool_and(
                  started_sequence < dispatch_sequence
                  AND dispatch_sequence < terminal_sequence
                )
                FROM tuge_model_invocation_projection
                WHERE invocation_id IN (
                  'inv-roundtrip-a',
                  'inv-roundtrip-b'
                )
                """
            ).fetchone()[0] is True
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conrelid = 'tuge_model_invocation_event'::regclass
                  AND conname = 'ck_tuge_model_event_terminal_semantics'
                """
            ).fetchone()[0] == 1
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conrelid = 'tuge_model_invocation_event'::regclass
                  AND conname = 'ck_tuge_model_event_schema'
                """
            ).fetchone()[0] == 1
            with pytest.raises(
                psycopg.errors.UniqueViolation,
            ):
                connection.execute(
                    """
                    INSERT INTO tuge_model_invocation_event (
                      event_id, server_sequence, event_type, logical_call_id,
                      invocation_id, attempt_no, dispatch_status,
                      occurred_at, ingested_at, tenant_id, feature_code,
                      provider, provider_request_id_hash, model_name,
                      privacy_mode, route_type, outcome, metadata_json
                    )
                    SELECT
                      'event-a-terminal-duplicate',
                      (
                        SELECT sequence_value + 1
                        FROM tuge_model_observability_sequence
                        WHERE sequence_name = 'event'
                      ),
                      'MODEL_INVOCATION_FAILED', logical_call_id,
                      invocation_id, attempt_no, 'DISPATCHED',
                      occurred_at + interval '1 second',
                      ingested_at + interval '1 second', tenant_id,
                      feature_code, provider, provider_request_id_hash,
                      model_name, privacy_mode, route_type, 'FAILURE',
                      '{}'::jsonb
                    FROM tuge_model_invocation_event
                    WHERE event_id = 'event-a-terminal'
                    """
                )
        finally:
            connection.execute("SET search_path TO public")
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


def test_postgres_migration_runner_detects_registry_checksum_tamper() -> None:
    schema_name = f"obs20_runner_{uuid.uuid4().hex}"
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    separator = "&" if "?" in sync_url else "?"
    scoped_url = (
        f"{sync_url}{separator}options=-csearch_path%3D{schema_name}"
    )
    migrations = discover_migrations()
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
    try:
        assert run_migrations(scoped_url) == [
            migration.version for migration in migrations
        ]
        assert run_migrations(scoped_url) == []

        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.execute(
                """
                UPDATE tuge_model_observability_schema_migration
                SET checksum = 'tampered-registry-checksum'
                WHERE version = '002_model_observability_hardening'
                """
            )

        with pytest.raises(
            ModelObservabilityMigrationError,
            match="002_model_observability_hardening changed after application",
        ):
            run_migrations(scoped_url)
    finally:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


@pytest.mark.asyncio
async def test_postgres_rebuild_blocks_concurrent_writes_without_losing_facts(
    postgres_session_factory,
    monkeypatch,
) -> None:
    lifecycle = ModelInvocationLifecycleService(postgres_session_factory)
    invocation = await lifecycle.start_invocation(
        _context(f"rebuild-race-{uuid.uuid4().hex}")
    )
    original_insert = ModelInvocationRepository.insert_projection_shadow
    rebuild_inside_shadow = asyncio.Event()
    release_rebuild = asyncio.Event()
    paused_once = False

    async def insert_then_pause(repository, table_name, projections):
        nonlocal paused_once
        await original_insert(repository, table_name, projections)
        if not paused_once:
            paused_once = True
            rebuild_inside_shadow.set()
            await release_rebuild.wait()

    monkeypatch.setattr(
        ModelInvocationRepository,
        "insert_projection_shadow",
        insert_then_pause,
    )
    rebuild_task = asyncio.create_task(lifecycle.rebuild_projection(batch_size=1))
    await asyncio.wait_for(rebuild_inside_shadow.wait(), timeout=5)

    async def dispatch_and_terminate() -> None:
        await lifecycle.mark_dispatched(
            invocation.invocation_id,
            provider_request_id="provider-request-during-rebuild",
        )
        await lifecycle.terminate(
            invocation.invocation_id,
            event_type=ModelInvocationEventType.SUCCEEDED,
            outcome=InvocationOutcome.SUCCESS,
            metrics=InvocationMetrics(
                input_tokens=8,
                output_tokens=4,
                latency_ms=19,
                time_to_first_token_ms=7,
            ),
        )

    writer_task = asyncio.create_task(dispatch_and_terminate())
    await asyncio.sleep(0.1)
    assert not writer_task.done()
    release_rebuild.set()
    await asyncio.wait_for(rebuild_task, timeout=5)
    await asyncio.wait_for(writer_task, timeout=5)

    events = await lifecycle.get_events(invocation.invocation_id)
    assert [event.event_type for event in events] == [
        ModelInvocationEventType.STARTED.value,
        ModelInvocationEventType.DISPATCHED.value,
        ModelInvocationEventType.SUCCEEDED.value,
    ]
    projection = await lifecycle.get_projection(invocation.invocation_id)
    projected_facts = (
        projection.lifecycle_status,
        projection.dispatch_status,
        projection.outcome,
        projection.provider_request_id_hash,
        projection.input_token_count,
        projection.output_token_count,
        projection.latency_ms,
        projection.started_sequence,
        projection.dispatch_sequence,
        projection.terminal_sequence,
    )
    assert projected_facts[:3] == ("TERMINAL", "DISPATCHED", "SUCCESS")
    assert projected_facts[3]

    assert await lifecycle.rebuild_projection(batch_size=1) >= 1
    rebuilt = await lifecycle.get_projection(invocation.invocation_id)
    assert (
        rebuilt.lifecycle_status,
        rebuilt.dispatch_status,
        rebuilt.outcome,
        rebuilt.provider_request_id_hash,
        rebuilt.input_token_count,
        rebuilt.output_token_count,
        rebuilt.latency_ms,
        rebuilt.started_sequence,
        rebuilt.dispatch_sequence,
        rebuilt.terminal_sequence,
    ) == projected_facts


def _database_url_for_schema(sync_url: str, schema_name: str) -> str:
    separator = "&" if "?" in sync_url else "?"
    return f"{sync_url}{separator}options=-csearch_path%3D{schema_name}"


def _seed_legacy_migration_registry(
    sync_url: str,
    schema_name: str,
    legacy_up_sql: str,
) -> None:
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
        )
        connection.execute(legacy_up_sql)
        connection.execute(
            """
            CREATE TABLE tuge_model_observability_schema_migration (
              version text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tuge_model_observability_schema_migration (
              version,
              checksum
            ) VALUES (
              '001_model_invocation_ledger',
              'a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be'
            )
            """
        )


def test_postgres_migration_runner_upgrades_only_exact_legacy_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import model_observability.migrate as migrate_module

    actual_migrations = discover_migrations()
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    for migration in actual_migrations:
        shutil.copyfile(migration.up_path, migration_dir / migration.up_path.name)
        shutil.copyfile(migration.down_path, migration_dir / migration.down_path.name)

    schema_name = f"obs20_legacy_ok_{uuid.uuid4().hex}"
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    _seed_legacy_migration_registry(
        sync_url,
        schema_name,
        actual_migrations[0].up_path.read_text("utf-8"),
    )
    monkeypatch.setattr(migrate_module, "MIGRATIONS_DIR", migration_dir)
    try:
        assert run_migrations(
            _database_url_for_schema(sync_url, schema_name)
        ) == ["002_model_observability_hardening"]
        with _sync_connection_for_schema(schema_name) as connection:
            rows = connection.execute(
                """
                SELECT version, checksum
                FROM tuge_model_observability_schema_migration
                ORDER BY version
                """
            ).fetchall()
            assert rows == [
                (
                    "001_model_invocation_ledger",
                    "480f02d221f9f4f4b35bfbce60e1b0ec337e135ae81944cf7955b7b2189a3da8",
                ),
                (
                    "002_model_observability_hardening",
                    discover_migrations()[1].checksum,
                ),
            ]
    finally:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


@pytest.mark.parametrize("tamper_kind", ["up", "down", "filename"])
def test_postgres_migration_runner_rejects_legacy_source_tamper_atomically(
    tmp_path: Path,
    monkeypatch,
    tamper_kind: str,
) -> None:
    import model_observability.migrate as migrate_module

    actual_migrations = discover_migrations()
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    for migration in actual_migrations:
        shutil.copyfile(migration.up_path, migration_dir / migration.up_path.name)
        shutil.copyfile(migration.down_path, migration_dir / migration.down_path.name)
    legacy = actual_migrations[0]
    if tamper_kind == "up":
        path = migration_dir / legacy.up_path.name
        path.write_bytes(path.read_bytes() + b"-- tampered up\n")
    elif tamper_kind == "down":
        path = migration_dir / legacy.down_path.name
        path.write_bytes(path.read_bytes() + b"-- tampered down\n")
    else:
        (migration_dir / legacy.down_path.name).rename(
            migration_dir / "001_model_invocation_ledger_renamed.down.sql"
        )

    schema_name = f"obs20_legacy_bad_{uuid.uuid4().hex}"
    sync_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    _seed_legacy_migration_registry(
        sync_url,
        schema_name,
        legacy.up_path.read_text("utf-8"),
    )
    monkeypatch.setattr(migrate_module, "MIGRATIONS_DIR", migration_dir)
    try:
        with pytest.raises(ModelObservabilityMigrationError):
            run_migrations(_database_url_for_schema(sync_url, schema_name))
        with _sync_connection_for_schema(schema_name) as connection:
            assert connection.execute(
                """
                SELECT checksum
                FROM tuge_model_observability_schema_migration
                WHERE version = '001_model_invocation_ledger'
                """
            ).fetchone()[0] == (
                "a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be"
            )
            assert connection.execute(
                "SELECT to_regclass('tuge_model_observability_sequence')"
            ).fetchone()[0] is None
    finally:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


@pytest.mark.asyncio
async def test_postgres_snapshot_global_count_capacity_is_atomic_and_reclaims_expired(
    postgres_session_factory,
) -> None:
    store = ModelSnapshotStore(
        postgres_session_factory,
        max_active_per_scope=5,
        max_active_global=4,
        max_total_bytes=1_024,
    )
    now = utc_now()

    async def create(index: int):
        return await store.create_or_reuse(
            scope_key=f"tenant-scope-{index}",
            query_hash=stable_query_hash({"query": index}),
            rows=[],
            snapshot_to=now,
            snapshot_mode="MATERIALIZED_RESULT_SET",
            max_ingested_at=None,
            max_event_id=None,
            max_sequence=None,
            data_through=None,
        )

    outcomes = await asyncio.gather(
        *(create(index) for index in range(8)),
        return_exceptions=True,
    )
    created = [
        outcome for outcome in outcomes
        if not isinstance(outcome, BaseException)
    ]
    rejected = [
        outcome for outcome in outcomes
        if isinstance(outcome, BaseException)
    ]
    assert len(created) == 4
    assert len(rejected) == 4
    assert all(isinstance(item, ModelSnapshotCapacityError) for item in rejected)
    assert {
        item.code for item in rejected
    } == {"OBSERVABILITY_SNAPSHOT_CAPACITY_EXCEEDED"}

    async with postgres_session_factory() as session:
        active_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) "
                    "FROM tuge_model_observability_query_snapshot "
                    "WHERE expires_at > now()"
                )
            )
        ).scalar_one()
        assert active_count == 4
        await session.execute(
            text(
                "UPDATE tuge_model_observability_query_snapshot "
                "SET expires_at = :expired "
                "WHERE high_watermark_handle = :handle"
            ),
            {
                "expired": utc_now() - timedelta(seconds=1),
                "handle": created[0].high_watermark_handle,
            },
        )

    replacement = await create(99)
    assert replacement.high_watermark_handle
    async with postgres_session_factory() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT COUNT(*), "
                    "COUNT(*) FILTER (WHERE expires_at > now()) "
                    "FROM tuge_model_observability_query_snapshot"
                )
            )
        ).one()
        assert tuple(counts) == (4, 4)


@pytest.mark.asyncio
async def test_postgres_orphan_reconciliation_is_conservative_idempotent_and_fences_late_worker(
    postgres_session_factory,
) -> None:
    lifecycle = ModelInvocationLifecycleService(postgres_session_factory)
    old = utc_now() - timedelta(hours=2)
    pre_boundary = await lifecycle.start_invocation(
        _context(f"orphan-pre-{uuid.uuid4().hex}"),
        occurred_at=old,
    )
    uncertain = await lifecycle.start_invocation(
        _context(f"orphan-uncertain-{uuid.uuid4().hex}"),
        occurred_at=old,
    )
    await lifecycle.mark_dispatch_unknown(
        uncertain.invocation_id,
        occurred_at=old,
    )

    results = await asyncio.gather(
        lifecycle.reconcile_orphans(
            older_than=timedelta(minutes=30),
            now=utc_now(),
        ),
        lifecycle.reconcile_orphans(
            older_than=timedelta(minutes=30),
            now=utc_now(),
        ),
    )
    assert sum(results) == 2
    assert await lifecycle.reconcile_orphans(
        older_than=timedelta(minutes=30),
        now=utc_now(),
    ) == 0

    pre_events = await lifecycle.get_events(pre_boundary.invocation_id)
    uncertain_events = await lifecycle.get_events(uncertain.invocation_id)
    assert [event.event_type for event in pre_events] == [
        "MODEL_INVOCATION_STARTED",
        "MODEL_INVOCATION_OUTCOME_UNKNOWN",
    ]
    assert [event.event_type for event in uncertain_events] == [
        "MODEL_INVOCATION_STARTED",
        "MODEL_INVOCATION_DISPATCH_UNKNOWN",
        "MODEL_INVOCATION_OUTCOME_UNKNOWN",
    ]
    assert pre_events[-1].dispatch_status == "DISPATCH_UNKNOWN"
    assert pre_events[-1].outcome == "UNKNOWN"

    with pytest.raises(InvalidInvocationTransitionError):
        await lifecycle.mark_dispatched(
            pre_boundary.invocation_id,
            provider_request_id="late-fenced-worker",
        )


@pytest.mark.asyncio
async def test_postgres_event_watermark_separates_ingestion_max_from_causal_tuple(
    postgres_session_factory,
    monkeypatch,
) -> None:
    lifecycle = ModelInvocationLifecycleService(postgres_session_factory)
    now = utc_now()
    higher_ingestion = now - timedelta(minutes=5)
    lower_ingestion = now - timedelta(minutes=10)
    first_suffix = f"watermark-first-{uuid.uuid4().hex}"
    second_suffix = f"watermark-second-{uuid.uuid4().hex}"

    monkeypatch.setattr(
        lifecycle_module,
        "utc_now",
        lambda: higher_ingestion,
    )
    first = await lifecycle.start_invocation(
        _context(first_suffix),
        occurred_at=now - timedelta(minutes=30),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "utc_now",
        lambda: lower_ingestion,
    )
    second = await lifecycle.start_invocation(
        _context(second_suffix),
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
        postgres_session_factory
    ).list_events(
        scope=ModelQueryScope(
            ("tenant-pg-integration",),
            "service-observability",
        ),
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
    assert result.watermark.max_ingested_at == ingestion_max.ingested_at
