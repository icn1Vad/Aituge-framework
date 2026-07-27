from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from common.system_constants import DEFAULT_TENANT_ID
from sqlalchemy import Column, DateTime, JSON, Text, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskEntity(SQLModel, table=True):
    __tablename__ = "tuge_task"
    __table_args__ = (
        UniqueConstraint("service", "tenant_id", "idempotency_key", name="unique_tuge_task_idempotency"),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    parent_task_id: Optional[str] = Field(default=None, foreign_key="tuge_task.id", max_length=80)
    root_task_id: Optional[str] = Field(default=None, max_length=80)
    task_key: Optional[str] = Field(default=None, max_length=160)
    idempotency_key: Optional[str] = Field(default=None, max_length=160)
    request_fingerprint: str = Field(default="", max_length=64)
    service: str = Field(default="external", max_length=80)
    task_type: str = Field(nullable=False, max_length=120)
    status: str = Field(default="created", max_length=32)
    title: str = Field(default="", sa_column=Column(Text))
    handler_name: str = Field(default="", max_length=80)
    input_payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_payload_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    error_payload_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    definition_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_schema_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    agent_id: str = Field(default="", max_length=80)
    thread_id: Optional[str] = Field(default=None, max_length=80)
    session_id: Optional[str] = Field(default=None, max_length=160)
    user_id: str = Field(default="default_user", max_length=120)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, max_length=64)
    stream_mode: bool = True
    current_run_id: Optional[str] = Field(default=None, max_length=80)
    attempt_count: int = 0
    progress_current: int = 0
    progress_total: int = 0
    cancel_requested: bool = False
    priority: int = 0
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    expires_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class TaskEventEntity(SQLModel, table=True):
    __tablename__ = "tuge_task_event"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_id: str = Field(foreign_key="tuge_task.id", nullable=False, max_length=80)
    run_id: Optional[str] = Field(default=None, max_length=80)
    schema_version: str = Field(default="1.0", max_length=16)
    parent_event_id: Optional[str] = Field(default=None, foreign_key="tuge_task_event.id", max_length=80)
    sequence: int = 0
    event_type: str = Field(nullable=False, max_length=80)
    level: str = Field(default="info", max_length=20)
    stage: str = Field(default="task_manager", max_length=80)
    step_id: Optional[str] = Field(default=None, max_length=120)
    step_index: Optional[int] = None
    item_id: Optional[str] = Field(default=None, max_length=80)
    stage_run_id: Optional[str] = Field(default=None, max_length=80)
    agent_id: Optional[str] = Field(default=None, max_length=80)
    tool_call_id: Optional[str] = Field(default=None, max_length=120)
    stream_semantics: str = Field(default="status", max_length=24)
    source_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    duration_ms: Optional[int] = None
    token_usage_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_code: Optional[str] = Field(default=None, max_length=80)
    visible: bool = True
    message: str = Field(default="", sa_column=Column(Text))
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))


class TaskItemEntity(SQLModel, table=True):
    __tablename__ = "tuge_task_item"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_id: str = Field(foreign_key="tuge_task.id", nullable=False, max_length=80)
    run_id: Optional[str] = Field(default=None, max_length=80)
    item_type: str = Field(default="item", max_length=80)
    item_key: str = Field(default="", max_length=160)
    sequence: int = 0
    status: str = Field(default="pending", max_length=32)
    input_payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_payload_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    error_payload_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))


class TaskRunEntity(SQLModel, table=True):
    __tablename__ = "tuge_task_run"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="unique_tuge_run_idempotency"),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_id: str = Field(foreign_key="tuge_task.id", nullable=False, index=True, max_length=80)
    idempotency_key: Optional[str] = Field(default=None, max_length=160)
    request_fingerprint: str = Field(default="", max_length=64)
    pipeline_id: str = Field(default="", max_length=120)
    pipeline_version: str = Field(default="", max_length=32)
    status: str = Field(default="pending", max_length=32)
    outcome: Optional[str] = Field(default=None, max_length=32)
    current_stage_id: Optional[str] = Field(default=None, max_length=120)
    cancel_requested: bool = False
    pause_requested: bool = False
    warning_count: int = 0
    error_code: Optional[str] = Field(default=None, max_length=120)
    error_message: str = Field(default="", sa_column=Column(Text))
    lease_owner: Optional[str] = Field(default=None, max_length=120)
    lease_until: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    lease_version: int = 0
    last_heartbeat_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    quota_slot_released: bool = False
    resource_pool: str = Field(default="default", max_length=80)
    next_event_sequence: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))


class TaskStageRunEntity(SQLModel, table=True):
    __tablename__ = "tuge_task_stage_run"
    __table_args__ = (
        UniqueConstraint("run_id", "stage_id", "attempt", name="unique_tuge_stage_attempt"),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_id: str = Field(foreign_key="tuge_task.id", nullable=False, index=True, max_length=80)
    run_id: str = Field(foreign_key="tuge_task_run.id", nullable=False, index=True, max_length=80)
    stage_id: str = Field(nullable=False, max_length=120)
    stage_type: str = Field(nullable=False, max_length=32)
    attempt: int = 1
    status: str = Field(default="pending", max_length=32)
    agent_id: Optional[str] = Field(default=None, max_length=80)
    thread_id: Optional[str] = Field(default=None, max_length=80)
    session_id: Optional[str] = Field(default=None, max_length=160)
    input_artifact_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    output_artifact_id: Optional[str] = Field(default=None, max_length=80)
    error_code: Optional[str] = Field(default=None, max_length=120)
    error_message: str = Field(default="", sa_column=Column(Text))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    duration_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))


class TaskArtifactEntity(SQLModel, table=True):
    __tablename__ = "tuge_task_artifact"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_id: str = Field(foreign_key="tuge_task.id", nullable=False, index=True, max_length=80)
    run_id: str = Field(foreign_key="tuge_task_run.id", nullable=False, index=True, max_length=80)
    stage_run_id: str = Field(foreign_key="tuge_task_stage_run.id", nullable=False, index=True, max_length=80)
    artifact_type: str = Field(nullable=False, max_length=120)
    artifact_version: int = 1
    schema_name: str = Field(default="", max_length=120)
    schema_version: str = Field(default="1.0", max_length=32)
    content_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    content_uri: Optional[str] = Field(default=None, sa_column=Column(Text))
    summary: str = Field(default="", sa_column=Column(Text))
    parent_artifact_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    checksum: str = Field(nullable=False, max_length=64)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))


class TaskMemoryEntity(SQLModel, table=True):
    """Immutable Task-scoped memory version for one tenant and user."""

    __tablename__ = "tuge_task_memory"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "task_key",
            "version",
            name="unique_tuge_task_memory_version",
        ),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True, max_length=64)
    user_id: str = Field(default="default_user", index=True, max_length=120)
    task_key: str = Field(nullable=False, index=True, max_length=160)
    version: int = Field(default=1, ge=1)
    content: str = Field(default="", sa_column=Column(Text))
    source_text: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))


_SQLITE_COLUMN_MIGRATIONS = {
    "tuge_task": {
        "parent_task_id": "VARCHAR(80)",
        "root_task_id": "VARCHAR(80)",
        "task_key": "VARCHAR(160)",
        "idempotency_key": "VARCHAR(160)",
        "request_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''",
        "service": "VARCHAR(80) NOT NULL DEFAULT 'external'",
        "handler_name": "VARCHAR(80) NOT NULL DEFAULT ''",
        "definition_snapshot_json": "JSON NOT NULL DEFAULT '{}'",
        "output_schema_json": "JSON NOT NULL DEFAULT '{}'",
        "progress_current": "INTEGER NOT NULL DEFAULT 0",
        "progress_total": "INTEGER NOT NULL DEFAULT 0",
        "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "expires_at": "DATETIME",
    },
    "tuge_task_event": {
        "schema_version": "VARCHAR(16) NOT NULL DEFAULT '1.0'",
        "parent_event_id": "VARCHAR(80)",
        "step_id": "VARCHAR(120)",
        "step_index": "INTEGER",
        "item_id": "VARCHAR(80)",
        "stage_run_id": "VARCHAR(80)",
        "agent_id": "VARCHAR(80)",
        "tool_call_id": "VARCHAR(120)",
        "stream_semantics": "VARCHAR(24) NOT NULL DEFAULT 'status'",
        "source_json": "JSON NOT NULL DEFAULT '{}'",
        "duration_ms": "INTEGER",
        "token_usage_json": "JSON NOT NULL DEFAULT '{}'",
        "error_code": "VARCHAR(80)",
        "visible": "BOOLEAN NOT NULL DEFAULT 1",
    },
    "tuge_task_run": {
        "request_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''",
        "lease_owner": "VARCHAR(120)",
        "lease_until": "DATETIME",
        "lease_version": "INTEGER NOT NULL DEFAULT 0",
        "last_heartbeat_at": "DATETIME",
        "quota_slot_released": "BOOLEAN NOT NULL DEFAULT 0",
        "resource_pool": "VARCHAR(80) NOT NULL DEFAULT 'default'",
        "next_event_sequence": "INTEGER NOT NULL DEFAULT 0",
    },
}


async def ensure_task_manager_schema(engine: AsyncEngine) -> None:
    """Add TaskManager columns and idempotency indexes to existing databases."""
    backend = engine.url.get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        return

    async with engine.begin() as conn:
        if backend == "sqlite":
            for table_name, migrations in _SQLITE_COLUMN_MIGRATIONS.items():
                result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                existing = {row[1] for row in result.fetchall()}
                for column_name, column_sql in migrations.items():
                    if column_name in existing:
                        continue
                    await conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {column_name} {column_sql}"
                        )
                    )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_tuge_task_service_tenant_idempotency "
                    "ON tuge_task (service, tenant_id, idempotency_key)"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_tuge_task_event_run_sequence "
                    "ON tuge_task_event (run_id, sequence) "
                    "WHERE run_id IS NOT NULL"
                )
            )
            await conn.execute(
                text(
                    "UPDATE tuge_task_run SET next_event_sequence = "
                    "COALESCE((SELECT MAX(sequence) FROM tuge_task_event "
                    "WHERE tuge_task_event.run_id = tuge_task_run.id), 0)"
                )
            )
            return
        await conn.execute(
            text(
                "ALTER TABLE tuge_task "
                "ADD COLUMN IF NOT EXISTS service VARCHAR(80) "
                "NOT NULL DEFAULT 'external'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE tuge_task "
                "ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64) "
                "NOT NULL DEFAULT ''"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE tuge_task_run "
                "ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64) "
                "NOT NULL DEFAULT ''"
            )
        )
        for column_name, column_sql in {
            "lease_owner": "VARCHAR(120)",
            "lease_until": "TIMESTAMP",
            "lease_version": "BIGINT NOT NULL DEFAULT 0",
            "last_heartbeat_at": "TIMESTAMP",
            "quota_slot_released": "BOOLEAN NOT NULL DEFAULT FALSE",
            "resource_pool": "VARCHAR(80) NOT NULL DEFAULT 'default'",
            "next_event_sequence": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            await conn.execute(
                text(
                    f"ALTER TABLE tuge_task_run ADD COLUMN IF NOT EXISTS "
                    f"{column_name} {column_sql}"
                )
            )
        await conn.execute(
            text(
                "ALTER TABLE tuge_task "
                "DROP CONSTRAINT IF EXISTS unique_tuge_task_idempotency"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_tuge_task_service_tenant_idempotency "
                "ON tuge_task (service, tenant_id, idempotency_key)"
            )
        )
        await conn.execute(
            text(
                "UPDATE tuge_task_run SET next_event_sequence = "
                "COALESCE((SELECT MAX(sequence) FROM tuge_task_event "
                "WHERE tuge_task_event.run_id = tuge_task_run.id), 0)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tuge_task_run_claim "
                "ON tuge_task_run (status, lease_until, created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tuge_task_event_run_sequence "
                "ON tuge_task_event (run_id, sequence) WHERE run_id IS NOT NULL"
            )
        )
