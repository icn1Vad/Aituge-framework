"""Single, explicit Page-8 entry point for Python observability migrations.

API and worker startup call assert_observability_ready only. Schema writes are
reserved for this module so concurrent processes cannot silently create a
partial observability schema.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

from db.db_context import get_engine
from model_observability.migrate import (
    MIGRATION_TABLE as MODEL_MIGRATION_TABLE,
)
from model_observability.migrate import (
    discover_migrations as discover_model_migrations,
)
from model_observability.migrate import (
    rollback_migration as rollback_model_migration,
)
from model_observability.migrate import (
    run_migrations as run_model_migrations,
)
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from task_manager.observability_internal.migration import (
    REVISION_ID as TASK_SECURITY_REVISION,
)
from task_manager.observability_internal.migration import (
    ensure_task_security_observability_schema,
    rollback_task_security_observability_schema,
)

_REQUIRED_MODEL_REVISIONS = frozenset(
    {
        "001_model_invocation_ledger",
        "002_model_observability_hardening",
    }
)
_REQUIRED_TABLES = frozenset(
    {
        "tuge_task",
        "tuge_task_run",
        "tuge_task_stage_run",
        "tuge_task_event",
        "tuge_model_invocation_event",
        "tuge_model_invocation_projection",
        "tuge_model_observability_query_snapshot",
        "tuge_model_observability_sequence",
        "tuge_security_audit_event",
        "tuge_obs30_scope_jti_claim",
        "tuge_obs30_query_snapshot",
    }
)
_REQUIRED_TASK_SECURITY_CONSTRAINTS = frozenset(
    {
        "ck_obs30_task_event_sequence",
        "ck_obs30_task_event_duration",
        "ck_obs30_task_event_time_order",
        "ck_obs30_snapshot_payload_bytes",
    }
)
_REQUIRED_TASK_SECURITY_INDEXES = frozenset(
    {
        "idx_obs30_task_event_tenant_occurred",
        "idx_obs30_task_event_tenant_ingested",
        "idx_obs30_task_event_task_sequence",
        "idx_obs30_task_event_run_sequence",
        "uq_obs30_task_event_run_sequence",
        "uq_obs30_task_event_task_sequence",
        "idx_obs30_task_event_request",
        "idx_obs30_task_event_trace",
        "idx_obs30_security_session",
        "idx_obs30_security_parent",
    }
)
_TEST_DATABASE_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,62}$")
_REQUIRED_TASK_EVENT_COLUMNS = frozenset(
    {
        "tenant_id",
        "request_id",
        "trace_id",
        "occurred_at",
        "ingested_at",
    }
)


class ObservabilityMigrationError(RuntimeError):
    """Stable migration/readiness failure without connection details."""


@dataclass(frozen=True, slots=True)
class ObservabilityMigrationResult:
    model_revisions_applied: tuple[str, ...]
    task_security_revision: str


@dataclass(frozen=True, slots=True)
class ObservabilityRollbackResult:
    model_revisions_rolled_back: tuple[str, ...]
    task_security_revision: str


async def migrate_observability(
    engine: AsyncEngine | None = None,
) -> ObservabilityMigrationResult:
    """Apply Page 2 then Page 3 to the Framework's one PostgreSQL database."""

    selected_engine = engine or get_engine()
    _require_postgresql(selected_engine)
    sync_url = _synchronous_postgres_url(selected_engine.url)
    model_revisions = await asyncio.to_thread(run_model_migrations, sync_url)
    await ensure_task_security_observability_schema(selected_engine)
    await assert_observability_ready(selected_engine)
    return ObservabilityMigrationResult(
        model_revisions_applied=tuple(model_revisions),
        task_security_revision=TASK_SECURITY_REVISION,
    )


async def rollback_observability(
    engine: AsyncEngine | None = None,
    *,
    confirm_isolated_database: str,
) -> ObservabilityRollbackResult:
    """Reverse Page 3 then Page 2 only for an explicitly confirmed test DB."""

    selected_engine = engine or get_engine()
    _require_postgresql(selected_engine)
    _require_isolated_test_database(
        selected_engine.url,
        confirmation=confirm_isolated_database,
    )
    await assert_observability_ready(selected_engine)
    await rollback_task_security_observability_schema(
        selected_engine,
        drop_task_event_context=True,
    )
    sync_url = _synchronous_postgres_url(selected_engine.url)
    rolled_back: list[str] = []
    for version in (
        "002_model_observability_hardening",
        "001_model_invocation_ledger",
    ):
        rolled_back.append(
            await asyncio.to_thread(
                rollback_model_migration,
                sync_url,
                version=version,
                confirm_version=version,
            )
        )
    return ObservabilityRollbackResult(
        model_revisions_rolled_back=tuple(rolled_back),
        task_security_revision=TASK_SECURITY_REVISION,
    )


async def assert_observability_ready(
    engine: AsyncEngine | None = None,
) -> None:
    """Fail fast when API/worker code is newer than the migrated schema."""

    selected_engine = engine or get_engine()
    _require_postgresql(selected_engine)
    async with selected_engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        if not _REQUIRED_TABLES.issubset(table_names):
            raise ObservabilityMigrationError("OBSERVABILITY_SCHEMA_NOT_READY")
        task_event_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("tuge_task_event")
            }
        )
        if not _REQUIRED_TASK_EVENT_COLUMNS.issubset(task_event_columns):
            raise ObservabilityMigrationError(
                "OBSERVABILITY_TASK_EVENT_SCHEMA_NOT_READY"
            )
        revision_rows = {
            str(row[0]): str(row[1])
            for row in (
                await connection.execute(
                    text(
                        f"SELECT version, checksum FROM {MODEL_MIGRATION_TABLE} "
                        "WHERE version IN "
                        "('001_model_invocation_ledger',"
                        "'002_model_observability_hardening')"
                    )
                )
            ).all()
        }
        migrations = {
            migration.version: migration
            for migration in discover_model_migrations()
            if migration.version in _REQUIRED_MODEL_REVISIONS
        }
        if (
            set(revision_rows) != _REQUIRED_MODEL_REVISIONS
            or set(migrations) != _REQUIRED_MODEL_REVISIONS
        ):
            raise ObservabilityMigrationError("OBSERVABILITY_MODEL_SCHEMA_NOT_READY")
        if any(
            not migrations[version].accepts_stored_checksum(checksum)
            for version, checksum in revision_rows.items()
        ):
            raise ObservabilityMigrationError(
                "OBSERVABILITY_MODEL_SCHEMA_CHECKSUM_INVALID"
            )
        constraints = {
            str(row[0])
            for row in (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conname = ANY(:names)"
                    ),
                    {"names": list(_REQUIRED_TASK_SECURITY_CONSTRAINTS)},
                )
            ).all()
        }
        if constraints != _REQUIRED_TASK_SECURITY_CONSTRAINTS:
            raise ObservabilityMigrationError(
                "OBSERVABILITY_TASK_SECURITY_CONSTRAINTS_NOT_READY"
            )
        indexes = {
            str(row[0])
            for row in (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND indexname = ANY(:names)"
                    ),
                    {"names": list(_REQUIRED_TASK_SECURITY_INDEXES)},
                )
            ).all()
        }
        if indexes != _REQUIRED_TASK_SECURITY_INDEXES:
            raise ObservabilityMigrationError(
                "OBSERVABILITY_TASK_SECURITY_INDEXES_NOT_READY"
            )
        append_function = (
            await connection.execute(
                text(
                    "SELECT to_regprocedure("
                    "'public.obs30_append_security_audit_event(jsonb)')"
                )
            )
        ).scalar_one_or_none()
        if append_function is None:
            raise ObservabilityMigrationError(
                "OBSERVABILITY_SECURITY_APPEND_FUNCTION_NOT_READY"
            )


def _require_postgresql(engine: AsyncEngine) -> None:
    if engine.url.get_backend_name() != "postgresql":
        raise ObservabilityMigrationError("OBSERVABILITY_POSTGRESQL_REQUIRED")


def _require_isolated_test_database(
    url: URL,
    *,
    confirmation: str,
) -> str:
    database = str(url.database or "")
    if (
        not database
        or _TEST_DATABASE_PATTERN.fullmatch(database) is None
        or database != confirmation
        or not any(marker in database.lower() for marker in ("test", "dev"))
        or any(
            marker in database.lower() for marker in ("formal", "prod", "production")
        )
    ):
        raise ObservabilityMigrationError(
            "OBSERVABILITY_ISOLATED_TEST_DATABASE_CONFIRMATION_REQUIRED"
        )
    return database


def _synchronous_postgres_url(url: URL) -> str:
    if url.get_backend_name() != "postgresql":
        raise ObservabilityMigrationError("OBSERVABILITY_POSTGRESQL_REQUIRED")
    sync_url = url.set(drivername="postgresql")
    # The value is passed directly to psycopg and is never logged or returned.
    return sync_url.render_as_string(hide_password=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply or check the shared Python observability schema"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--confirm-isolated-database")
    args = parser.parse_args()

    async def run() -> None:
        if args.apply:
            result = await migrate_observability()
            print(
                "Observability migrations applied; "
                f"model_count={len(result.model_revisions_applied)} "
                f"task_security={result.task_security_revision}"
            )
        elif args.rollback:
            result = await rollback_observability(
                confirm_isolated_database=args.confirm_isolated_database or "",
            )
            print(
                "Observability migrations rolled back; "
                f"model_count={len(result.model_revisions_rolled_back)} "
                f"task_security={result.task_security_revision}"
            )
        else:
            await assert_observability_ready()
            print("Observability schema is ready")

    asyncio.run(run())


if __name__ == "__main__":
    main()
