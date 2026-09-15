"""An idempotent message-reference ledger; tombstones reject delayed events."""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, JSON

class FileReferenceOwner(SQLModel, table=True):
    __tablename__ = "tuge_attachment_reference_owner"
    tenant_id: str = Field(primary_key=True, max_length=64)
    owner_id: str = Field(primary_key=True, max_length=128)
    revision: int = Field(default=0)
    file_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
