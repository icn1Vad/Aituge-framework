from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeContextBlock:
    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SchedulingRuntimeContext:
    blocks: tuple[RuntimeContextBlock, ...] = ()

    def render_prompt(self) -> str:
        return "\n\n".join(
            block.content.strip()
            for block in self.blocks
            if block.content and block.content.strip()
        )

    def extend(self, *blocks: RuntimeContextBlock) -> "SchedulingRuntimeContext":
        return SchedulingRuntimeContext(blocks=self.blocks + tuple(blocks))

    def combine(self, other: "SchedulingRuntimeContext | None") -> "SchedulingRuntimeContext":
        if other is None:
            return self
        return SchedulingRuntimeContext(blocks=self.blocks + other.blocks)
