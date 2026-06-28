from pathlib import Path

import pytest

from tool.registry import ToolProviderConfig, ToolList, get_default_tool_list


def test_default_tool_list_contains_current_tool_providers():
    tool_list = get_default_tool_list()
    entries = {(entry.tool_name, entry.provider) for entry in tool_list.list()}

    assert ("code_interpreter", "local_python") in entries
    assert ("code_interpreter", "limited_sandbox") in entries


def test_default_tool_list_exposes_llm_tool_names():
    tool_list = get_default_tool_list()

    local_python = tool_list.get("code_interpreter", "local_python")
    limited_sandbox = tool_list.get("code_interpreter", "limited_sandbox")

    assert local_python.llm_tool_names == ("LimitedLocalPythonInterpreter",)
    assert limited_sandbox.llm_tool_names == (
        "LimitedPythonInterpreter",
        "LimitedInstallPythonPackage",
    )


def test_tool_list_builds_local_python_bundle(tmp_path: Path):
    bundle = get_default_tool_list().create_bundle(
        ToolProviderConfig(
            tool_name="code_interpreter",
            provider="local_python",
            config={"work_dir": tmp_path, "keep_work_dir": True},
        )
    )

    assert [tool.metadata.name for tool in bundle.tools] == [
        "LimitedLocalPythonInterpreter"
    ]
    assert len(bundle.cleanup_hooks) == 1


def test_tool_list_returns_empty_bundle_when_disabled():
    bundle = get_default_tool_list().create_bundle(
        ToolProviderConfig(
            tool_name="code_interpreter",
            provider="local_python",
            enabled=False,
        )
    )

    assert bundle.tools == []
    assert bundle.cleanup_hooks == []


def test_tool_list_raises_for_unknown_provider():
    tool_list = ToolList()

    with pytest.raises(KeyError):
        tool_list.get("web_search", "aliyun")
