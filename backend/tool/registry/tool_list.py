"""Default ToolList containing concrete providers shipped in this workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tool.local_runtime import (
    LimitedLocalPythonConfig,
    create_limited_local_python_bundle,
)
from tool.search import AliyunSearchConfig, create_aliyun_web_search_bundle

from .config import ToolProviderConfig
from .registry import ToolDefinition, ToolList


def _coerce_path(value: Any) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def _create_local_python_bundle(config: ToolProviderConfig):
    raw = dict(config.config)
    if "work_dir" in raw:
        raw["work_dir"] = _coerce_path(raw["work_dir"])
    runtime_config = LimitedLocalPythonConfig(**raw)
    return create_limited_local_python_bundle(runtime_config)


def _create_aliyun_web_search_bundle(config: ToolProviderConfig):
    raw = {**dict(config.config), **dict(config.secrets)}
    search_config = AliyunSearchConfig(**raw)
    return create_aliyun_web_search_bundle(search_config)


def create_default_tool_list() -> ToolList:
    tool_list = ToolList()
    tool_list.register(
        ToolDefinition(
            tool_name="code_interpreter",
            provider="local_python",
            display_name="Local Python Interpreter",
            description="Run trusted local Python code and collect stdout/artifacts.",
            llm_tool_names=("LimitedLocalPythonInterpreter",),
            factory=_create_local_python_bundle,
        ),
        make_default=True,
    )
    tool_list.register(
        ToolDefinition(
            tool_name="web_search",
            provider="aliyun",
            display_name="Aliyun IQS Web Search",
            description="Search the web with Aliyun IQS and return PAI-style JSON results.",
            llm_tool_names=("aliyun-websearch",),
            factory=_create_aliyun_web_search_bundle,
        ),
        make_default=True,
    )
    return tool_list


DEFAULT_TOOL_LIST = create_default_tool_list()


def get_default_tool_list() -> ToolList:
    return DEFAULT_TOOL_LIST
