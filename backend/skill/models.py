"""Skill data models.

Skills are prompt resources selected by a task. They are intentionally not
callable tools: task code loads them, then renders their content into the
task prompt passed to a single agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str = ""
    version: str | None = None
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Skill:
    metadata: SkillMetadata
    content: str
    path: Path

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description


@dataclass(frozen=True, slots=True)
class SkillSummary:
    name: str
    description: str
    path: Path
    tags: list[str] = field(default_factory=list)
