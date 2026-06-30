from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from common.system_constants import DEFAULT_TENANT_ID
from sqlalchemy import Column, DateTime, JSON, Text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskEntity(SQLModel, table=True):
    __tablename__ = "tuge_task"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_type: str = Field(nullable=False, max_length=120)
    status: str = Field(default="created", max_length=32)
    title: str = Field(default="", sa_column=Column(Text))
    input_payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_payload_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    error_payload_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    agent_id: str = Field(default="", max_length=80)
    thread_id: Optional[str] = Field(default=None, max_length=80)
    session_id: Optional[str] = Field(default=None, max_length=160)
    user_id: str = Field(default="default_user", max_length=120)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, max_length=64)
    stream_mode: bool = True
    current_run_id: Optional[str] = Field(default=None, max_length=80)
    attempt_count: int = 0
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class TaskEventEntity(SQLModel, table=True):
    __tablename__ = "tuge_task_event"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    task_id: str = Field(foreign_key="tuge_task.id", nullable=False, max_length=80)
    run_id: Optional[str] = Field(default=None, max_length=80)
    sequence: int = 0
    event_type: str = Field(nullable=False, max_length=80)
    level: str = Field(default="info", max_length=20)
    stage: str = Field(default="task_manager", max_length=80)
    message: str = Field(default="", sa_column=Column(Text))
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
