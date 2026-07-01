"""SQLModel entities for task-owned skill packages."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from common.system_constants import DEFAULT_TENANT_ID
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def _json_loads(value: str, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class SkillPackage(SQLModel):
    tenant_id: Optional[str] = Field(default=DEFAULT_TENANT_ID, max_length=64)
    package_name: str = Field(max_length=80)
    display_name: str = Field(default="", max_length=120)
    description: str = ""
    tags_json: str = Field(default="[]")
    primary_skill: str = Field(max_length=120)
    auxiliary_skills_json: str = Field(default="[]")
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SkillPackageEntity(SkillPackage, table=True):
    __tablename__ = "tuge_skill_package"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "package_name",
            name="unique_tuge_skill_package",
        ),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=64)

    @property
    def tags(self) -> list[str]:
        value = _json_loads(self.tags_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def auxiliary_skills(self) -> list[str]:
        value = _json_loads(self.auxiliary_skills_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    def to_read_model(self) -> dict[str, Any]:
        return {
            "package_name": self.package_name,
            "display_name": self.display_name,
            "description": self.description,
            "tags": self.tags,
            "primary_skill": self.primary_skill,
            "auxiliary_skills": self.auxiliary_skills,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
