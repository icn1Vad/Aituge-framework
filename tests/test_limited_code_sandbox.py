import pytest

from tool.sandbox import LimitedCodeSandboxConfig, LimitedCodeSandboxTool
from tool.sandbox.factory import (
    create_limited_code_sandbox_bundle,
    create_limited_code_sandbox_tools,
)
from tool.sandbox.limited_code_sandbox_exceptions import (
    LimitedCodeSandboxNotConfiguredException,
    LimitedCodeSandboxTimeoutException,
)


def _enabled_config(**overrides):
    values = {
        "enabled": True,
        "base_url": "http://sandbox.example.test",
        "interpreter_name": "python",
        "timeout_default": 3,
        "max_output_chars": 80,
    }
    values.update(overrides)
    return LimitedCodeSandboxConfig(**values)


def test_limited_sandbox_factory_uses_limited_tool_names():
    tools, cleanup = create_limited_code_sandbox_tools(_enabled_config())

    assert [tool.metadata.name for tool in tools] == [
        "LimitedPythonInterpreter",
        "LimitedInstallPythonPackage",
    ]
    assert callable(cleanup)


def test_limited_sandbox_bundle_wraps_tools_and_cleanup():
    bundle = create_limited_code_sandbox_bundle(_enabled_config())

    assert [tool.metadata.name for tool in bundle.tools] == [
        "LimitedPythonInterpreter",
        "LimitedInstallPythonPackage",
    ]
    assert len(bundle.cleanup_hooks) == 1


def test_extract_code_accepts_raw_json_fence_and_xml():
    tool = LimitedCodeSandboxTool(_enabled_config())

    assert tool._extract_code("print('hi')") == "print('hi')"
    assert tool._extract_code('{"code": "print(1)"}') == "print(1)"
    assert tool._extract_code({"raw": "print(2)"}) == "print(2)"
    assert tool._extract_code({"code": "```python\nprint(3)\n```"}) == "print(3)"
    assert tool._extract_code({"code": "<code>print(4)</code>"}) == "print(4)"


def test_format_execute_result_matches_pai_style_sections():
    tool = LimitedCodeSandboxTool(_enabled_config())

    result = tool._format_execute_result(
        {
            "results": [
                {"type": "stdout", "text": "hello\n"},
                {"type": "result", "text": "42"},
                {"type": "stderr", "text": "warn\n"},
                {"type": "error", "name": "ValueError", "value": "bad"},
            ]
        }
    )

    assert "stdout:\nhello" in result
    assert "result:\n42" in result
    assert "stderr:\nwarn" in result
    assert "error:\nValueError: bad" in result


def test_format_execute_result_raises_on_timeout_marker():
    tool = LimitedCodeSandboxTool(_enabled_config())

    with pytest.raises(LimitedCodeSandboxTimeoutException):
        tool._format_execute_result({"results": [{"type": "timeout"}]})


def test_disabled_sandbox_fails_before_remote_call():
    tool = LimitedCodeSandboxTool(
        LimitedCodeSandboxConfig(
            enabled=False,
            base_url="http://sandbox.example.test",
            interpreter_name="python",
        )
    )

    with pytest.raises(LimitedCodeSandboxNotConfiguredException):
        tool._validate_configured()
