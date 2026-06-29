from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, JSON, Text
from sqlmodel import Field, SQLModel


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DiscussionRunEntity(SQLModel, table=True):
    __tablename__ = "tuge_discussion_run"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    public_thread_id: str = Field(nullable=False, max_length=80)
    user_id: str = Field(default="default_user", max_length=120)
    topic: str = Field(sa_column=Column(Text))
    participant_agent_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    moderator_agent_id: Optional[str] = Field(default=None, max_length=80)
    status: str = Field(default="pending", max_length=32)
    max_rounds: int = 1
    current_round: int = 0
    final_output: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))


class DiscussionParticipantEntity(SQLModel, table=True):
    __tablename__ = "tuge_discussion_participant"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    run_id: str = Field(foreign_key="tuge_discussion_run.id", nullable=False, max_length=80)
    agent_id: str = Field(nullable=False, max_length=80)
    agent_session_id: str = Field(nullable=False, max_length=160)
    agent_thread_id: str = Field(nullable=False, max_length=80)
    agent_profile_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    position: int = 0
    enabled: bool = True
    created_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))


class DiscussionTurnEntity(SQLModel, table=True):
    __tablename__ = "tuge_discussion_turn"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    run_id: str = Field(foreign_key="tuge_discussion_run.id", nullable=False, max_length=80)
    round_index: int = 0
    turn_index: int = 0
    agent_id: str = Field(nullable=False, max_length=80)
    action: Optional[str] = Field(default=None, max_length=32)
    reason: str = Field(default="", sa_column=Column(Text))
    public_message_id: Optional[str] = Field(default=None, max_length=80)
    private_thread_id: Optional[str] = Field(default=None, max_length=80)
    private_session_id: Optional[str] = Field(default=None, max_length=160)
    status: str = Field(default="pending", max_length=32)
    response_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    tool_steps_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    skills_json: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    error: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime))
