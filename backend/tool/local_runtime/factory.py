"""Factories for local runtime tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from llama_index.core.tools.function_tool import FunctionTool

from tool.bundle import ToolBundle

from .limited_local_python import LimitedLocalPythonConfig, LimitedLocalPythonTool


CleanupHook = Callable[[], Awaitable[None]]


def create_limited_local_python_tools(
    config: LimitedLocalPythonConfig | None = None,
) -> tuple[list[FunctionTool], CleanupHook]:
    """Create a minimal local Python interpreter tool plus cleanup."""

    python_tool = LimitedLocalPythonTool(config)

    async def aexecute_limited_local_python(code: str) -> str:
        return await python_tool.aexecute(code)

    execute_tool = FunctionTool.from_defaults(
        async_fn=aexecute_limited_local_python,
        name="LimitedLocalPythonInterpreter",
        description="""Execute Python code locally in a temporary working directory and return exit code, stdout, and stderr.

# Use when
- The user asks to run, verify, calculate, test, or debug Python code.
- You need to check the output of a small Python snippet before answering.

# Limits
- This is a convenience local runner, not a hardened security sandbox.
- The caller config controls timeout, Python executable, working directory, and output length.
- Always print values that should be visible in the final result.
- Save user-visible artifacts with relative paths in the current working directory, such as "report.html" or "chart.png"; do not save artifacts to /tmp or absolute paths.

# Parameters
- code (required, string): Python code to execute. Raw code, {"code": "..."}, Markdown code fences, and <code>...</code> are accepted.
""",
        return_direct=False,
    )

    return [execute_tool], python_tool.acleanup


def create_limited_local_python_bundle(
    config: LimitedLocalPythonConfig | None = None,
) -> ToolBundle:
    """Create the local Python interpreter as a task-owned bundle."""

    tools, cleanup = create_limited_local_python_tools(config)
    return ToolBundle.from_tools(tools, cleanup=cleanup)
