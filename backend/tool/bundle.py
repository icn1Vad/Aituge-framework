"""Small utility container for task-owned tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Optional

from llama_index.core.tools.function_tool import FunctionTool


CleanupHook = Callable[[], Awaitable[None] | None]


async def _run_cleanup(cleanup: CleanupHook) -> None:
    result = cleanup()
    if result is not None:
        await result


@dataclass(slots=True)
class ToolBundle:
    """Tools selected by the task layer for one agent run.

    The bundle intentionally does not decide permissions or selection policy.
    It is only a transport object: callers build it outside the single-agent
    runtime, pass `tools` into the runner, and call `cleanup` when the run ends.
    """

    tools: list[FunctionTool] = field(default_factory=list)
    cleanup_hooks: list[CleanupHook] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "ToolBundle":
        return cls()

    @classmethod
    def from_tools(
        cls,
        tools: Iterable[FunctionTool],
        cleanup: Optional[CleanupHook] = None,
    ) -> "ToolBundle":
        cleanup_hooks = [cleanup] if cleanup else []
        return cls(tools=list(tools), cleanup_hooks=cleanup_hooks)

    @classmethod
    def combine(cls, bundles: Iterable["ToolBundle"]) -> "ToolBundle":
        combined = cls()
        for bundle in bundles:
            combined.tools.extend(bundle.tools)
            combined.cleanup_hooks.extend(bundle.cleanup_hooks)
        return combined

    def add_cleanup(self, cleanup: CleanupHook) -> None:
        self.cleanup_hooks.append(cleanup)

    async def cleanup(self) -> None:
        for cleanup_hook in reversed(self.cleanup_hooks):
            await _run_cleanup(cleanup_hook)
