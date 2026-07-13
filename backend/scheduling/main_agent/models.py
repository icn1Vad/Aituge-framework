from __future__ import annotations

import uuid
from datetime import datetime, timezone

from common.system_constants import DEFAULT_TENANT_ID
from sqlalchemy import Column, DateTime, Text, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MainAgentSessionEntity(SQLModel, table=True):
    """Minimal scheduling state for one MainAgent conversation."""

    __tablename__ = "tuge_main_agent_session"

    session_id: str = Field(primary_key=True, max_length=160)
    thread_id: str | None = Field(default=None, index=True, max_length=80)
    phase: str = Field(default="new", max_length=32)
    user_id: str = Field(default="default_user", index=True, max_length=120)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True, max_length=64)
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))

    def to_read_model(self) -> dict:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "phase": self.phase,
        }


class ManagedSingleAgentEntity(SQLModel, table=True):
    """A Single Agent kept by one MainAgent conversation."""

    __tablename__ = "tuge_main_agent_managed_agent"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "primary_session_id",
            "instance_id",
            name="unique_tuge_main_agent_instance",
        ),
    )

    instance_id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    primary_session_id: str = Field(index=True, max_length=160)
    primary_thread_id: str | None = Field(default=None, index=True, max_length=80)
    primary_agent_id: str = Field(max_length=80)
    agent_id: str = Field(max_length=80)
    child_thread_id: str | None = Field(default=None, max_length=80)
    child_session_id: str = Field(max_length=160)
    user_id: str = Field(default="default_user", max_length=120)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, max_length=64)
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))

    def to_read_model(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "agent_id": self.agent_id,
            "thread_id": self.child_thread_id,
            "session_id": self.child_session_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ScriptWorkspaceEntity(SQLModel, table=True):
    """Minimal shared text used by the first MainAgent demo."""

    __tablename__ = "tuge_script_workspace"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=80)
    user_id: str = Field(default="default_user", index=True, max_length=120)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True, max_length=64)
    script_text: str = Field(default="", sa_column=Column(Text))
    storyboard_text: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=Column(DateTime))

    def to_read_model(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "script_text": self.script_text,
            "storyboard_text": self.storyboard_text,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


async def ensure_main_agent_schema(engine: AsyncEngine) -> None:
    if engine.url.get_backend_name() != "sqlite":
        return
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(tuge_script_workspace)"))
        existing = {row[1] for row in result.fetchall()}
        if existing and "storyboard_text" not in existing:
            await conn.execute(
                text("ALTER TABLE tuge_script_workspace ADD COLUMN storyboard_text TEXT NOT NULL DEFAULT ''")
            )
