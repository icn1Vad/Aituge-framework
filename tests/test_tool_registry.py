from pathlib import Path

import pytest

from common.encrypt_utils import encrypt_key
from tool.registry import (
    ToolConfigEntity,
    ToolProviderConfig,
    ToolList,
    get_default_tool_list,
)


def test_default_tool_list_contains_current_tool_providers():
    tool_list = get_default_tool_list()
    entries = {(entry.tool_name, entry.provider) for entry in tool_list.list()}

    assert ("code_interpreter", "local_python") in entries
    assert ("code_interpreter", "limited_sandbox") in entries
    assert ("web_search", "aliyun") in entries


def test_default_tool_list_exposes_llm_tool_names():
    tool_list = get_default_tool_list()

    local_python = tool_list.get("code_interpreter", "local_python")
    limited_sandbox = tool_list.get("code_interpreter", "limited_sandbox")

    assert local_python.llm_tool_names == ("LimitedLocalPythonInterpreter",)
    assert limited_sandbox.llm_tool_names == (
        "LimitedPythonInterpreter",
        "LimitedInstallPythonPackage",
    )
    assert tool_list.get("web_search", "aliyun").llm_tool_names == (
        "aliyun-websearch",
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


def test_tool_config_entity_to_provider_config_decrypts_secrets():
    entity = ToolConfigEntity(
        tool_name="web_search",
        provider="aliyun",
        config_json='{"endpoint": "iqs.cn-zhangjiakou.aliyuncs.com", "search_count": 5}',
        encrypted_secrets_json=encrypt_key(
            '{"access_key_id": "ak", "access_key_secret": "sk"}'
        ),
    )

    config = entity.to_provider_config()

    assert config.tool_name == "web_search"
    assert config.provider == "aliyun"
    assert config.config["search_count"] == 5
    assert config.secrets == {"access_key_id": "ak", "access_key_secret": "sk"}
