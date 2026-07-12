from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, JSON, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SmartFillTask(SQLModel, table=True):
    __tablename__ = "smart_fill_task"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    task_name: str = Field(sa_column=Column(Text))
    user_id: str = "default_user"
    document_id: str | None = None
    report_file_name: str | None = None
    report_file_size: int | None = None
    parse_status: str = "pending"
    fill_status: str = "pending"
    review_status: str = "reviewing"
    section_count: int = 0
    table_count: int = 0
    task_manager_id: str | None = None
    created_at: datetime = Field(default_factory=now, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=now, sa_column=Column(DateTime))


class SmartFillFieldValue(SQLModel, table=True):
    __tablename__ = "smart_fill_field_value"
    __table_args__ = (UniqueConstraint("task_id", "field_id", name="unique_smart_fill_task_field"),)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    task_id: str = Field(foreign_key="smart_fill_task.id", index=True)
    field_id: str = Field(index=True)
    field_name: str = ""
    value_json: Any = Field(default=None, sa_column=Column(JSON))
    status: str = "missing"
    is_manual_modified: bool = False
    is_confirmed: bool = False
    updated_at: datetime = Field(default_factory=now, sa_column=Column(DateTime))


class SmartFillEvidenceRecord(SQLModel, table=True):
    __tablename__ = "smart_fill_evidence"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    task_id: str = Field(foreign_key="smart_fill_task.id", index=True)
    field_id: str = Field(index=True)
    evidence_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class SmartFillAuditRecord(SQLModel, table=True):
    __tablename__ = "smart_fill_audit"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    task_id: str = Field(foreign_key="smart_fill_task.id", index=True)
    field_id: str = Field(index=True)
    action: str
    old_value_json: Any = Field(default=None, sa_column=Column(JSON))
    new_value_json: Any = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now, sa_column=Column(DateTime))
