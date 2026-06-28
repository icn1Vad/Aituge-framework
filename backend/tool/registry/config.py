"""Shared config objects for tool registry entries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ToolProviderConfig:
    """Runtime config used to build one concrete tool provider."""

    tool_name: str
    provider: str
    enabled: bool = True
    config: Mapping[str, Any] = field(default_factory=dict)
    secrets: Mapping[str, Any] = field(default_factory=dict)

