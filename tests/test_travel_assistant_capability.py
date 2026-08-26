from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


class _Registry:
    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}

    def register_local_tool(self, **kwargs) -> None:
        pass

    def register_agent(self, **kwargs) -> None:
        self.agents[kwargs["agent_id"]] = kwargs

    def register_resource_task(self, **kwargs) -> None:
        self.tasks[kwargs["task_type"]] = kwargs


def _load_module():
    entry = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "travel-assistant"
        / "capabilities"
        / "register.py"
    )
    spec = importlib.util.spec_from_file_location("travel_assistant_capability_test", entry)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_assistant_can_query_policies_without_starting_a_form(monkeypatch):
    module = _load_module()
    registry = _Registry()
    runtime = SimpleNamespace(active_pack=SimpleNamespace(llm=SimpleNamespace(id="api-model")))
    monkeypatch.setattr(
        module.ModelRuntimeProvider,
        "from_environment",
        lambda **kwargs: runtime,
    )

    asyncio.run(module.register(registry, {"MODEL_PACK_ID": "api-rerank"}))

    expected_tools = [
        "start_workflow",
        "apply_form_changes",
        "proof_search",
    ]
    agent = registry.agents["workflow-assistant-agent"]
    task = registry.tasks["workflow.assistant.chat"]

    assert agent["default_tools"] == expected_tools
    assert task["default_tools"] == expected_tools
    assert "二者只能选择一条路径" in agent["system_prompt"]
    assert "我打算去北京出趟差" in agent["system_prompt"]
    assert "绝对不能调用 proof_search" in agent["system_prompt"]
    assert "只有用户明确询问制度依据" in agent["system_prompt"]
    assert "制度咨询过程中不得调用 start_workflow" in agent["system_prompt"]
    assert "才调用 proof_search" in agent["system_prompt"]
    assert "不得继续尝试其他数据工具" in agent["system_prompt"]
    assert "proof_sql" not in agent["system_prompt"]
    assert "必须结合完整会话历史" in agent["system_prompt"]
    assert "禁止展示字段键或 MEETING" in agent["system_prompt"]
    assert "当前北京时间日期" in agent["system_prompt"]
    assert "activityType 只用于活动分类，绝对不能替代 notes" in agent["system_prompt"]
    assert "要去开一个学术会议" in agent["system_prompt"]

    trip_fields = {field.key: field for field in module.TRIP_WORKFLOW.fields}
    assert "applicationDate" not in trip_fields
    assert "tripDays" in trip_fields
    assert "activityType" in trip_fields
    assert any("同时写 activityType 和具体 notes" in item for item in module.TRIP_WORKFLOW.instructions)
    assert trip_fields["budgetYear"].enum_values == (str(module._beijing_today().year),)
