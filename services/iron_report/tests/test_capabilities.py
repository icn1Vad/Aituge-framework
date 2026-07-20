import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


REGISTER_PATH = Path(__file__).resolve().parents[1] / "capabilities" / "register.py"
SKILL_PATH = REGISTER_PATH.parent / "skills" / "iron-report-generator" / "SKILL.md"


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

    assert [tool["tool_name"] for tool in registry.tools] == [
        "iron_market_data",
        "iron_report_research",
    ]
    assert registry.agents[0]["model_id"] == "configured-model"
    assert registry.agents[0]["default_tools"] == [
        "iron_market_data",
        "iron_report_research",
        "code_interpreter",
    ]
    system_prompt = registry.agents[0]["system_prompt"]
    for field in (
        "title",
        "executive_summary",
        "metrics",
        "sections",
        "sources",
        "research_status",
    ):
        assert f'"{field}"' in system_prompt
    assert "所有面向读者的报告内容必须使用简体中文" in system_prompt
    assert "JSON 属性名、枚举、标识符" in system_prompt
    assert registry.tasks[0]["task_type"] == "iron.report.generate"
    assert registry.tasks[0]["handler"] == "scheduler"
    assert registry.tasks[0]["result_sink_url"].endswith(
        "/v1/internal/iron-report/task-result?callback_token=internal-token-123"
    )

    payload = registry.tasks[0]["input_model"].model_validate(
        {
            "report_type": "DAILY",
            "report_date": "2026-07-17",
            "data_as_of_date": "2026-07-17",
            "output_formats": ["DOCX", "PDF"],
            "include_web_research": True,
        }
    ).model_dump()
    assert payload["report_date"] == "2026-07-17"
    assert payload["data_as_of_date"] == "2026-07-17"
    json.dumps(payload)


def test_skill_and_image_define_chinese_chart_font_contract() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    matched_font = subprocess.run(
        ["fc-match", "Noto Sans CJK SC", "--format=%{family}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Every chart title, axis label, legend, and annotation must use Simplified Chinese" in skill
    assert "Noto Sans CJK SC" in skill
    assert "axes.unicode_minus" in skill
    assert "Noto Sans CJK" in matched_font
