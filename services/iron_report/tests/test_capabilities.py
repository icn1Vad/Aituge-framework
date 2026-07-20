import importlib.util
from pathlib import Path

import pytest


REGISTER_PATH = Path(__file__).resolve().parents[1] / "capabilities" / "register.py"


class FakeRegistry:
    def __init__(self) -> None:
        self.skill_roots = []
        self.tools = []
        self.packages = []
        self.agents = []
        self.tasks = []

    def register_skill_root(self, path):
        self.skill_roots.append(path)

    def register_http_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_skill_package(self, **kwargs):
        self.packages.append(kwargs)

    def register_agent(self, **kwargs):
        self.agents.append(kwargs)

    def register_task(self, **kwargs):
        self.tasks.append(kwargs)


class FakeSettings:
    values = {
        "IRON_REPORT_SERVICE_BASE_URL": "http://iron-report-demo-python:18300",
        "IRON_REPORT_MODEL_ID": "configured-model",
        "IRON_REPORT_INTERNAL_TOKEN": "internal-token-123",
    }

    def require(self, name: str) -> str:
        return self.values[name]


def _module():
    spec = importlib.util.spec_from_file_location("iron_report_capability", REGISTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_capability_uses_configured_model_and_existing_react_tools() -> None:
    registry = FakeRegistry()

    await _module().register(registry, FakeSettings())

    assert registry.tools[0]["tool_name"] == "iron_market_data"
    assert registry.agents[0]["model_id"] == "configured-model"
    assert registry.agents[0]["default_tools"] == ["iron_market_data", "web_search", "code_interpreter"]
    assert registry.tasks[0]["task_type"] == "iron.report.generate"
    assert registry.tasks[0]["handler"] == "scheduler"
    assert registry.tasks[0]["result_sink_url"].endswith(
        "/v1/internal/iron-report/task-result?callback_token=internal-token-123"
    )
