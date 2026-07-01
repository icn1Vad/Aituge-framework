from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel import Field, SQLModel


def _json_loads(value: str, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class AgentProfileEntity(SQLModel, table=True):
    """Stored configuration for one schedulable single agent."""

    __tablename__ = "tuge_agent_profile"

    agent_id: str = Field(primary_key=True, max_length=80)
    name: str = Field(max_length=120)
    description: str = ""
    agent_type: str = Field(default="single", max_length=32)
    model_id: str = Field(default="deepseek-v4-pro", max_length=120)
    system_prompt: str = ""
    default_tools_json: str = Field(default="[]")
    default_datasets_json: str = Field(default="[]")
    runtime_config_json: str = Field(default="{}")
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def default_tools(self) -> list[str]:
        value = _json_loads(self.default_tools_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def default_datasets(self) -> list[str]:
        value = _json_loads(self.default_datasets_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def runtime_config(self) -> dict[str, Any]:
        value = _json_loads(self.runtime_config_json, {})
        return dict(value) if isinstance(value, dict) else {}

    def to_read_model(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "agent_type": self.agent_type,
            "model_id": self.model_id,
            "system_prompt": self.system_prompt,
            "default_tools": self.default_tools,
            "default_datasets": self.default_datasets,
            "runtime_config": self.runtime_config,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
