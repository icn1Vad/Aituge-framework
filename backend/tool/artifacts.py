"""Shared artifact contract for tools and task-owned publishers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    id: str
    name: str
    mime: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ArtifactPublisher(Protocol):
    async def publish(
        self,
        source_path: Path,
        *,
        sequence: int,
        mime: str,
    ) -> ArtifactRef: ...


def extract_artifacts(value: Any) -> list[dict[str, str]]:
    """Read the bounded public artifact list from a structured tool result."""

    if not isinstance(value, str):
        return []
    try:
        raw_items = json.loads(value).get("artifacts") or []
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return []

    artifacts: list[dict[str, str]] = []
    for raw in raw_items[:20]:
        if not isinstance(raw, dict):
            continue
        item = {
            field: str(raw.get(field) or "")[:2000]
            for field in ("id", "name", "mime", "url")
        }
        if not item["id"] or not item["url"]:
            continue
        artifacts.append(item)
    return artifacts


def extract_step_artifacts(steps: Any) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        for item in extract_artifacts(step.get("result")):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            artifacts.append(item)
    return artifacts
