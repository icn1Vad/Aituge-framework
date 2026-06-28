import uuid
from datetime import datetime, timezone
from typing import List, Optional

from common.system_constants import DEFAULT_TENANT_ID
from pydantic import field_serializer
from sqlalchemy import Column, DateTime, JSON
from sqlmodel import Field, SQLModel


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MessageCreate(SQLModel):
    local_id: Optional[str] = Field(default=None)
    thread_id: str = Field(default=None)
    role: str = Field(default=None)
    content: List[dict] = Field(default_factory=list, sa_column=Column("content", JSON))
    attachments: List[dict] = Field(default_factory=list, sa_column=Column("attachments", JSON))
    token_usage: Optional[dict] = Field(default=None, sa_column=Column("token_usage", JSON))
    created_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))


class MessageRead(MessageCreate):
    id: str = Field(default=None, primary_key=True)


class MessageEntity(SQLModel, table=True):
    __tablename__ = "tuge_message"

    id: str = Field(default_factory=lambda: str(uuid.uuid4().hex), primary_key=True)
    thread_id: str = Field(
        default=None,
        foreign_key="tuge_thread.id",
        ondelete="CASCADE",
        nullable=False,
    )
    local_id: Optional[str] = Field(default=None)
    tenant_id: Optional[str] = Field(default=DEFAULT_TENANT_ID)

    role: str = Field(default=None)
    content: List[dict] = Field(default_factory=list, sa_column=Column("content", JSON))
    attachments: List[dict] = Field(default_factory=list, sa_column=Column("attachments", JSON))
    token_usage: Optional[dict] = Field(default=None, sa_column=Column("token_usage", JSON))
    created_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))

    @field_serializer("created_at")
    def serialize_dt(self, dt: datetime, _info):
        if dt.tzinfo is None:
            return f"{dt.isoformat()}Z"
        return dt.isoformat()
