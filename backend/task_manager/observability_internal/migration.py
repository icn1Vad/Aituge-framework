from __future__ import annotations

import json
import re

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from .models import (
    QuerySnapshotEntity,
    ScopeJtiClaimEntity,
    SecurityAuditEventEntity,
)

REVISION_ID = "obs30_task_security_20260802"

TASK_EVENT_CONTEXT_COLUMNS: dict[str, str] = {
    "tenant_id": "VARCHAR(64)",
    "user_id": "VARCHAR(120)",
    "request_id": "VARCHAR(120)",
    "trace_id": "VARCHAR(64)",
    "span_id": "VARCHAR(32)",
    "privacy_mode": "VARCHAR(16)",
    "route_type": "VARCHAR(16)",
    "service_name": "VARCHAR(80)",
    "service_version": "VARCHAR(80)",
    "environment": "VARCHAR(32)",
    "occurred_at": "TIMESTAMP",
    "ingested_at": "TIMESTAMP",
}

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_obs30_task_event_tenant_occurred "
    "ON tuge_task_event (tenant_id, occurred_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_obs30_task_event_tenant_ingested "
    "ON tuge_task_event (tenant_id, ingested_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_obs30_task_event_task_sequence "
    "ON tuge_task_event (task_id, sequence, id)",
    "CREATE INDEX IF NOT EXISTS idx_obs30_task_event_run_sequence "
    "ON tuge_task_event (run_id, sequence, id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_obs30_task_event_run_sequence "
    "ON tuge_task_event (run_id, sequence) WHERE run_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_obs30_task_event_task_sequence "
    "ON tuge_task_event (task_id, sequence) WHERE run_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_obs30_task_event_request "
    "ON tuge_task_event (request_id) WHERE request_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_obs30_task_event_trace "
    "ON tuge_task_event (trace_id) WHERE trace_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_obs30_security_session "
    "ON tuge_security_audit_event (access_session_id, occurred_at, id) "
    "WHERE access_session_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_obs30_security_parent "
    "ON tuge_security_audit_event (parent_audit_event_id) "
    "WHERE parent_audit_event_id IS NOT NULL",
)

POSTGRES_TASK_EVENT_CONSTRAINTS = """
DO $obs30$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_obs30_task_event_sequence'
  ) THEN
    ALTER TABLE tuge_task_event
      ADD CONSTRAINT ck_obs30_task_event_sequence CHECK (sequence > 0) NOT VALID;
    ALTER TABLE tuge_task_event VALIDATE CONSTRAINT ck_obs30_task_event_sequence;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_obs30_task_event_duration'
  ) THEN
    ALTER TABLE tuge_task_event
      ADD CONSTRAINT ck_obs30_task_event_duration
      CHECK (duration_ms IS NULL OR duration_ms >= 0) NOT VALID;
    ALTER TABLE tuge_task_event VALIDATE CONSTRAINT ck_obs30_task_event_duration;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_obs30_task_event_time_order'
  ) THEN
    ALTER TABLE tuge_task_event
      ADD CONSTRAINT ck_obs30_task_event_time_order
      CHECK (occurred_at <= ingested_at) NOT VALID;
    ALTER TABLE tuge_task_event VALIDATE CONSTRAINT ck_obs30_task_event_time_order;
  END IF;
END
$obs30$
"""
POSTGRES_SECURITY_APPEND_FUNCTION = """
CREATE OR REPLACE FUNCTION public.obs30_append_security_audit_event(
  p_event jsonb
)
RETURNS SETOF public.tuge_security_audit_event
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $obs30$
DECLARE
  existing_row public.tuge_security_audit_event%ROWTYPE;
BEGIN
  INSERT INTO public.tuge_security_audit_event (
    id,
    idempotency_key,
    immutable_fingerprint,
    audit_action_id,
    access_session_id,
    audit_layer,
    parent_audit_event_id,
    scope_type,
    schema_version,
    service,
    tenant_id,
    occurred_at,
    ingested_at,
    category,
    action,
    risk_level,
    actor_type,
    actor_id,
    subject_type,
    subject_id,
    missing_context_reason,
    cross_tenant,
    reason_code,
    source_ip_masked,
    request_id,
    trace_id,
    outcome,
    error_code,
    display_code,
    metadata_json
  )
  VALUES (
    p_event->>'id',
    p_event->>'idempotency_key',
    p_event->>'immutable_fingerprint',
    p_event->>'audit_action_id',
    p_event->>'access_session_id',
    'PYTHON_EXECUTION',
    p_event->>'parent_audit_event_id',
    p_event->>'scope_type',
    (p_event->>'schema_version')::integer,
    p_event->>'service',
    p_event->>'tenant_id',
    (p_event->>'occurred_at')::timestamp without time zone,
    (p_event->>'ingested_at')::timestamp without time zone,
    p_event->>'category',
    p_event->>'action',
    p_event->>'risk_level',
    p_event->>'actor_type',
    p_event->>'actor_id',
    p_event->>'subject_type',
    p_event->>'subject_id',
    p_event->>'missing_context_reason',
    COALESCE((p_event->>'cross_tenant')::boolean, false),
    p_event->>'reason_code',
    p_event->>'source_ip_masked',
    p_event->>'request_id',
    p_event->>'trace_id',
    p_event->>'outcome',
    p_event->>'error_code',
    p_event->>'display_code',
    COALESCE((p_event->'metadata_json')::json, '[]'::json)
  )
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING * INTO existing_row;

  IF existing_row.id IS NULL THEN
    SELECT *
      INTO STRICT existing_row
      FROM public.tuge_security_audit_event
     WHERE idempotency_key = p_event->>'idempotency_key';
    IF existing_row.immutable_fingerprint
       IS DISTINCT FROM p_event->>'immutable_fingerprint' THEN
      RAISE EXCEPTION USING
        ERRCODE = 'P3001',
        MESSAGE = 'security audit immutable replay conflict';
    END IF;
  END IF;

  RETURN NEXT existing_row;
END
$obs30$;
"""

ROLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


async def ensure_task_security_observability_schema(engine: AsyncEngine) -> None:
    """Apply the independent Page-3 revision to SQLite or PostgreSQL."""

    backend = engine.url.get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        raise ValueError(
            "Page-3 observability revision supports SQLite and PostgreSQL only."
        )

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SecurityAuditEventEntity.__table__.create(
                bind=sync_conn, checkfirst=True
            )
        )
        await conn.run_sync(
            lambda sync_conn: ScopeJtiClaimEntity.__table__.create(
                bind=sync_conn, checkfirst=True
            )
        )
        await conn.run_sync(
            lambda sync_conn: QuerySnapshotEntity.__table__.create(
                bind=sync_conn, checkfirst=True
            )
        )

        tables = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        required_tables = {
            "tuge_task",
            "tuge_task_run",
            "tuge_task_stage_run",
            "tuge_task_event",
        }
        if not required_tables.issubset(tables):
            raise RuntimeError("Page-3 migration requires the Task Manager schema")

        security_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns(
                    "tuge_security_audit_event"
                )
            }
        )
        if "immutable_fingerprint" not in security_columns:
            existing_count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM tuge_security_audit_event")
                )
            ).scalar_one()
            if existing_count:
                raise RuntimeError(
                    "existing security events require reviewed fingerprint backfill"
                )
            await conn.execute(
                text(
                    "ALTER TABLE tuge_security_audit_event ADD COLUMN "
                    "immutable_fingerprint VARCHAR(64)"
                )
            )
            if backend == "postgresql":
                await conn.execute(
                    text(
                        "ALTER TABLE tuge_security_audit_event "
                        "ALTER COLUMN immutable_fingerprint SET NOT NULL"
                    )
                )
        if backend == "postgresql":
            await conn.execute(
                text(
                    "ALTER TABLE tuge_security_audit_event "
                    "ALTER COLUMN source_ip_masked TYPE VARCHAR(80)"
                )
            )

        snapshot_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns(
                    "tuge_obs30_query_snapshot"
                )
            }
        )
        if "payload_bytes" not in snapshot_columns:
            await conn.execute(
                text(
                    "ALTER TABLE tuge_obs30_query_snapshot "
                    "ADD COLUMN payload_bytes INTEGER NOT NULL DEFAULT 0"
                )
            )
            snapshot_rows = (
                await conn.execute(
                    text("SELECT handle, payload_json FROM tuge_obs30_query_snapshot")
                )
            ).all()
            for handle, payload in snapshot_rows:
                if isinstance(payload, str):
                    payload = json.loads(payload)
                payload_bytes = len(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
                await conn.execute(
                    text(
                        "UPDATE tuge_obs30_query_snapshot "
                        "SET payload_bytes = :payload_bytes "
                        "WHERE handle = :handle"
                    ),
                    {"payload_bytes": payload_bytes, "handle": handle},
                )

        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("tuge_task_event")
            }
        )
        for column_name, column_type in TASK_EVENT_CONTEXT_COLUMNS.items():
            if column_name not in columns:
                await conn.execute(
                    text(
                        f"ALTER TABLE tuge_task_event ADD COLUMN "
                        f"{column_name} {column_type}"
                    )
                )

        await conn.execute(
            text(
                "UPDATE tuge_task_event SET "
                "tenant_id = COALESCE(tenant_id, "
                "(SELECT tenant_id FROM tuge_task "
                "WHERE tuge_task.id = tuge_task_event.task_id)), "
                "user_id = COALESCE(user_id, "
                "(SELECT user_id FROM tuge_task "
                "WHERE tuge_task.id = tuge_task_event.task_id)), "
                "service_name = COALESCE(service_name, "
                "(SELECT service FROM tuge_task "
                "WHERE tuge_task.id = tuge_task_event.task_id)), "
                "occurred_at = COALESCE(occurred_at, created_at), "
                "ingested_at = COALESCE(ingested_at, created_at)"
            )
        )

        mismatch_count = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM tuge_task_event e "
                    "LEFT JOIN tuge_task t ON t.id = e.task_id "
                    "LEFT JOIN tuge_task_run r ON r.id = e.run_id "
                    "LEFT JOIN tuge_task_stage_run s ON s.id = e.stage_run_id "
                    "WHERE t.id IS NULL OR e.tenant_id IS NULL "
                    "OR e.tenant_id <> t.tenant_id "
                    "OR (e.run_id IS NOT NULL AND "
                    "(r.id IS NULL OR r.task_id <> e.task_id)) "
                    "OR (e.stage_run_id IS NOT NULL AND "
                    "(s.id IS NULL OR s.task_id <> e.task_id "
                    "OR e.run_id IS NULL OR s.run_id <> e.run_id))"
                )
            )
        ).scalar_one()
        if mismatch_count:
            raise RuntimeError(
                "Task/Run/Stage/Event ownership mismatch blocks Page-3 migration"
            )

        if backend == "postgresql":
            await conn.execute(
                text(
                    "ALTER TABLE tuge_security_audit_event "
                    "DROP CONSTRAINT IF EXISTS ck_obs30_security_time_order"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tuge_task_event "
                    "ALTER COLUMN tenant_id SET NOT NULL, "
                    "ALTER COLUMN occurred_at SET NOT NULL, "
                    "ALTER COLUMN ingested_at SET NOT NULL"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE tuge_obs30_query_snapshot "
                    "ALTER COLUMN payload_bytes SET NOT NULL"
                )
            )
            await conn.execute(text(POSTGRES_TASK_EVENT_CONSTRAINTS))
            await conn.execute(
                text(
                    "DO $obs30$ BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'ck_obs30_snapshot_payload_bytes') THEN "
                    "ALTER TABLE tuge_obs30_query_snapshot ADD CONSTRAINT "
                    "ck_obs30_snapshot_payload_bytes CHECK (payload_bytes >= 0) "
                    "NOT VALID; ALTER TABLE tuge_obs30_query_snapshot VALIDATE "
                    "CONSTRAINT ck_obs30_snapshot_payload_bytes; END IF; "
                    "END $obs30$"
                )
            )

            await conn.execute(text(POSTGRES_SECURITY_APPEND_FUNCTION))
            await conn.execute(
                text(
                    "REVOKE ALL ON FUNCTION "
                    "public.obs30_append_security_audit_event(jsonb) FROM PUBLIC"
                )
            )
        for statement in INDEX_STATEMENTS:
            await conn.execute(text(statement))


async def configure_security_audit_postgres_roles(
    engine: AsyncEngine,
    *,
    append_role: str,
    read_role: str,
    retention_role: str,
) -> None:
    """Apply the least-privilege audit-table grants after deployment roles exist."""

    if engine.url.get_backend_name() != "postgresql":
        raise ValueError("security audit role grants require PostgreSQL")
    roles = (append_role, read_role, retention_role)
    if len(set(roles)) != len(roles):
        raise ValueError("security audit append/read/retention roles must be distinct")
    if not all(ROLE_NAME_PATTERN.fullmatch(role) for role in roles):
        raise ValueError("invalid PostgreSQL role name")

    quoted_roles = tuple(f'"{role}"' for role in roles)
    async with engine.begin() as conn:
        for role in roles:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                    {"role": role},
                )
            ).scalar_one_or_none()
            if exists is None:
                raise RuntimeError(f"required PostgreSQL role does not exist: {role}")

        await conn.execute(
            text("REVOKE ALL ON TABLE public.tuge_security_audit_event FROM PUBLIC")
        )
        await conn.execute(
            text(
                "REVOKE ALL ON FUNCTION "
                "public.obs30_append_security_audit_event(jsonb) FROM PUBLIC"
            )
        )
        for role in quoted_roles:
            await conn.execute(
                text(
                    f"REVOKE ALL ON TABLE public.tuge_security_audit_event FROM {role}"
                )
            )
            await conn.execute(
                text(
                    "REVOKE ALL ON FUNCTION "
                    "public.obs30_append_security_audit_event(jsonb) "
                    f"FROM {role}"
                )
            )
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))

        await conn.execute(
            text(
                "GRANT INSERT ON TABLE public.tuge_security_audit_event "
                f"TO {quoted_roles[0]}"
            )
        )
        await conn.execute(
            text(
                "GRANT EXECUTE ON FUNCTION "
                "public.obs30_append_security_audit_event(jsonb) "
                f"TO {quoted_roles[0]}"
            )
        )
        await conn.execute(
            text(
                "GRANT SELECT ON TABLE public.tuge_security_audit_event "
                f"TO {quoted_roles[1]}"
            )
        )
        await conn.execute(
            text(
                "GRANT SELECT, DELETE ON TABLE public.tuge_security_audit_event "
                f"TO {quoted_roles[2]}"
            )
        )


async def rollback_task_security_observability_schema(
    engine: AsyncEngine,
    *,
    drop_task_event_context: bool = False,
) -> None:
    """Rollback Page-3 objects only in an isolated maintenance window."""

    backend = engine.url.get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        raise ValueError(
            "Page-3 observability rollback supports SQLite and PostgreSQL only."
        )
    async with engine.begin() as conn:
        for statement in reversed(INDEX_STATEMENTS):
            index_name = statement.split("INDEX IF NOT EXISTS ", 1)[1].split(" ", 1)[0]
            await conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        if backend == "postgresql":
            await conn.execute(
                text(
                    "DROP FUNCTION IF EXISTS "
                    "public.obs30_append_security_audit_event(jsonb)"
                )
            )
            for constraint in (
                "ck_obs30_task_event_time_order",
                "ck_obs30_task_event_duration",
                "ck_obs30_task_event_sequence",
            ):
                await conn.execute(
                    text(
                        f"ALTER TABLE tuge_task_event "
                        f"DROP CONSTRAINT IF EXISTS {constraint}"
                    )
                )
        await conn.execute(text("DROP TABLE IF EXISTS tuge_obs30_query_snapshot"))
        await conn.execute(text("DROP TABLE IF EXISTS tuge_obs30_scope_jti_claim"))
        await conn.execute(text("DROP TABLE IF EXISTS tuge_security_audit_event"))
        if drop_task_event_context:
            for column_name in reversed(tuple(TASK_EVENT_CONTEXT_COLUMNS)):
                await conn.execute(
                    text(f"ALTER TABLE tuge_task_event DROP COLUMN {column_name}")
                )
