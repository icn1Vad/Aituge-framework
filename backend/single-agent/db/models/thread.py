import uuid
from datetime import datetime, timezone
from typing import Optional

from common.system_constants import DEFAULT_TENANT_ID
from pydantic import field_serializer
from sqlalchemy import Column, DateTime, Text
from sqlmodel import Field, SQLModel


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ThreadCreate(SQLModel):
    user_id: str = Field(default="TUGE Assistant")
    title: Optional[str] = Field(default=None, sa_column=Column(Text))


class ThreadRead(ThreadCreate):
    id: str = Field(default=None, primary_key=True)
    archived: bool = Field(default=False)


class ThreadEntity(SQLModel, table=True):
    __tablename__ = "tuge_thread"

    id: str = Field(default_factory=lambda: str(uuid.uuid4().hex), primary_key=True)
    user_id: str = Field(default="TUGE Assistant", nullable=False)
    title: Optional[str] = Field(default=None, sa_column=Column(Text))
    tenant_id: Optional[str] = Field(default=DEFAULT_TENANT_ID)

    created_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))
    updated_at: datetime = Field(default_factory=_utc_now_naive, sa_column=Column(DateTime))

    @field_serializer("created_at", "updated_at")
    def serialize_dt(self, dt: datetime, _info):
        if dt.tzinfo is None:
            return f"{dt.isoformat()}Z"
        return dt.isoformat()
