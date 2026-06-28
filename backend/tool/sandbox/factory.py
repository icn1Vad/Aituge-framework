"""Factory helpers for limited PAI-style sandbox FunctionTools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from llama_index.core.tools.function_tool import FunctionTool
from loguru import logger

from .limited_code_sandbox import LimitedCodeSandboxConfig, LimitedCodeSandboxTool
from .limited_code_sandbox_exceptions import (
    LimitedCodeSandboxException,
    LimitedCodeSandboxNotConfiguredException,
)
from tool.bundle import ToolBundle


CleanupHook = Callable[[], Awaitable[None]]


def create_limited_code_sandbox_tools(
    sandbox_config: LimitedCodeSandboxConfig,
) -> tuple[list[FunctionTool], CleanupHook]:
    """Create limited code sandbox tools plus a cleanup hook.

    This mirrors PAI-RAG's `create_codesandbox_tools`: one sandbox client is
    created for a conversation/task, multiple tools share that client, and the
    caller owns cleanup when the run finishes.
    """

    code_tool = LimitedCodeSandboxTool(sandbox_config)

    async def aexecute_limited_python(code: str) -> str:
        if code_tool is None:
            raise LimitedCodeSandboxNotConfiguredException("Limited sandbox not configured.")
        try:
            return await code_tool.aexecute(code)
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            logger.exception("Limited code sandbox execution failed.")
            raise exc

    execute_tool = FunctionTool.from_defaults(
        async_fn=aexecute_limited_python,
        name="LimitedPythonInterpreter",
        description="""Execute Python code in a limited remote sandbox and return printed output. Use this tool only for bounded calculations, spreadsheet-style analysis, or simple data visualization work.

# Limits
- This is a limited execution helper, not an unrestricted operating system.
- The caller config controls timeout and maximum returned output size.
- Only printed stdout, expression results, stderr, and execution errors are returned.
- Always use explicit print(...) for values you need to inspect.
- For charts, save files and print their filenames; file download/export is not enabled in this minimal version.

# Parameters
- code (required, string): Python code to execute. The value may be raw code, a JSON string with {"code": "..."}, a Markdown code fence, or <code>...</code>.
""",
        return_direct=False,
    )

    async def ainstall_limited_python_package(package_name: str) -> str:
        if code_tool is None:
            raise LimitedCodeSandboxNotConfiguredException("Limited sandbox not configured.")
        try:
            result = await code_tool.ainstall_package(package_name)
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            logger.exception("Limited code sandbox package installation failed.")
            raise exc
        return str(result.get("status", "unknown"))

    install_tool = FunctionTool.from_defaults(
        async_fn=ainstall_limited_python_package,
        name="LimitedInstallPythonPackage",
        description="""Install a Python package in the limited remote sandbox with sudo pip install.

# Limits
- Use only when execution genuinely needs an extra package.
- The package is installed only inside the current sandbox instance.
- Package installation is bounded by the sandbox command timeout and remote environment policy.

# Parameters
- package_name (required, string): Package spec such as "numpy", "pandas==2.2.2", or "numpy pandas matplotlib".
""",
        return_direct=False,
    )

    async def cleanup_limited_code_sandbox() -> None:
        try:
            await code_tool.adelete_sandbox_instance()
        except Exception as exc:
            logger.exception(f"Failed to delete limited code sandbox instance: {exc}")
            await code_tool.aclose()

    return [execute_tool, install_tool], cleanup_limited_code_sandbox


def create_limited_code_sandbox_bundle(
    sandbox_config: LimitedCodeSandboxConfig,
) -> ToolBundle:
    """Create limited code sandbox tools as a task-owned bundle."""

    tools, cleanup = create_limited_code_sandbox_tools(sandbox_config)
    return ToolBundle.from_tools(tools, cleanup=cleanup)
