"""SQLModel entities for tool provider configuration."""

from __future__ import annotations

import json
import uuid
from typing import Optional

from common.encrypt_utils import decrypt_key
from common.system_constants import DEFAULT_TENANT_ID
from sqlmodel import Field, SQLModel
from sqlalchemy import UniqueConstraint

from .config import ToolProviderConfig


class ToolConfig(SQLModel):
    tenant_id: Optional[str] = Field(default=DEFAULT_TENANT_ID, max_length=64)
    tool_name: str = Field(max_length=64)
    provider: str = Field(max_length=64)
    enabled: bool = Field(default=True)
    config_json: str = Field(default="{}")
    encrypted_secrets_json: str = Field(default="")


class ToolConfigEntity(ToolConfig, table=True):
    __tablename__ = "tuge_tool_config"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "tool_name",
            "provider",
            name="unique_tuge_tool_config",
        ),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=64)

    def to_provider_config(self) -> ToolProviderConfig:
        config = _load_json_object(self.config_json)
        secrets = (
            _load_json_object(decrypt_key(self.encrypted_secrets_json))
            if self.encrypted_secrets_json
            else {}
        )
        return ToolProviderConfig(
            tool_name=self.tool_name,
            provider=self.provider,
            enabled=self.enabled,
            config=config,
            secrets=secrets,
        )


def _load_json_object(value: str | None) -> dict:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("tool config JSON must decode to an object")
    return loaded
