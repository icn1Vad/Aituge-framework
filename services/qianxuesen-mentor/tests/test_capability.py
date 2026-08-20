import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_capability():
    path = Path(__file__).parents[1] / "capabilities" / "register.py"
    spec = importlib.util.spec_from_file_location("qxs_capability", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeRegistry:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def capture(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return capture


class FakeSettings:
    def require(self, name):
        assert name == "QXS_SERVICE_BASE_URL"
        return "http://qxs:18400"

    def get(self, _name):
        return ""


def test_capability_registers_task_tools_and_skill(monkeypatch):
    capability = load_capability()
    runtime = SimpleNamespace(active_pack=SimpleNamespace(llm=SimpleNamespace(id="llm-test")))
    monkeypatch.setattr(
        capability.ModelRuntimeProvider, "from_environment", classmethod(lambda cls, **kwargs: runtime)
    )
    registry = FakeRegistry()
    asyncio.run(capability.register(registry, FakeSettings()))
    tool_names = {kwargs["tool_name"] for name, _args, kwargs in registry.calls if name == "register_http_tool"}
    task_types = {kwargs["task_type"] for name, _args, kwargs in registry.calls if name == "register_resource_task"}
    agent = next(kwargs for name, _args, kwargs in registry.calls if name == "register_agent")
    assert tool_names == {"qxs_retrieve", "qxs_sql"}
    assert task_types == {"qianxuesen.qa.chat"}
    assert {"qxs_retrieve", "qxs_sql", "code_interpreter", "web_search"} <= set(agent["default_tools"])
