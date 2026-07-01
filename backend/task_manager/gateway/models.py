from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GatewayResourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["kb", "file", "table", "api"]
    id: str = Field(min_length=1, max_length=200)
    operation: Literal["search", "read", "query", "call"] = "read"
    scope: str = "task"
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    task_id: str | None = None
    resource_type: Literal["kb", "file", "table", "api"]
    operation: Literal["search", "read", "query", "call"]
    resource_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class GatewayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "denied", "not_found", "error"]
    data: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    access_scope: dict[str, Any] = Field(default_factory=dict)
