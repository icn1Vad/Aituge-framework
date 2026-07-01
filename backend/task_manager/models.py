from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from common.system_constants import DEFAULT_TENANT_ID
from sqlalchemy import Column, DateTime, JSON, Text, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskEntity(SQLModel, table=True):
    __tablename__ = "tuge_task"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    parent_task_id: Optional[str] = Field(default=None, foreign_key="tuge_task.id", max_length=80)
    root_task_id: Optional[str] = Field(default=None, max_length=80)
    task_key: Optional[str] = Field(default=None, max_length=160)
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
    parent_event_id: Optional[str] = Field(default=None, foreign_key="tuge_task_event.id", max_length=80)
    sequence: int = 0
    event_type: str = Field(nullable=False, max_length=80)
    level: str = Field(default="info", max_length=20)
    stage: str = Field(default="task_manager", max_length=80)
    step_id: Optional[str] = Field(default=None, max_length=120)
    step_index: Optional[int] = None
    item_id: Optional[str] = Field(default=None, max_length=80)
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


_SQLITE_COLUMN_MIGRATIONS = {
    "tuge_task": {
        "parent_task_id": "VARCHAR(80)",
        "root_task_id": "VARCHAR(80)",
        "task_key": "VARCHAR(160)",
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
        "parent_event_id": "VARCHAR(80)",
        "step_id": "VARCHAR(120)",
        "step_index": "INTEGER",
        "item_id": "VARCHAR(80)",
        "duration_ms": "INTEGER",
        "token_usage_json": "JSON NOT NULL DEFAULT '{}'",
        "error_code": "VARCHAR(80)",
        "visible": "BOOLEAN NOT NULL DEFAULT 1",
    },
}


async def ensure_task_manager_schema(engine: AsyncEngine) -> None:
    """Add TaskManager v1 columns to existing local SQLite databases."""
    if engine.url.get_backend_name() != "sqlite":
        return

    async with engine.begin() as conn:
        for table_name, migrations in _SQLITE_COLUMN_MIGRATIONS.items():
            result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
            existing = {row[1] for row in result.fetchall()}
            for column_name, column_sql in migrations.items():
                if column_name in existing:
                    continue
                await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))
