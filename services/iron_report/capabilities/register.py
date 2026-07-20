"""Declare iron ore report capabilities without modifying Framework core."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from iron_report.schemas import IronReportTaskOutput, IronReportTaskPayload, ReportType


CAPABILITY_ID = "iron-report"
CAPABILITY_DIR = Path(__file__).resolve().parent


class IronMarketDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: ReportType
    report_date: date


class IronReportResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=400)
    report_date: date


async def register(registry, settings) -> None:
    service_base_url = settings.require("IRON_REPORT_SERVICE_BASE_URL").rstrip("/")
    model_id = settings.require("IRON_REPORT_MODEL_ID")
    callback_token = quote(settings.require("IRON_REPORT_INTERNAL_TOKEN"), safe="")
    output_schema = json.dumps(
        IronReportTaskOutput.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    registry.register_skill_root(CAPABILITY_DIR / "skills")
    registry.register_http_tool(
        tool_name="iron_market_data",
        provider="iron_report_http",
        display_name="Iron Ore Prepared Market Data",
        description=(
            "Load the validated iron ore market snapshot, time series, fixed fallback news, "
            "source cards, and limitations for one daily or weekly report date."
        ),
        base_url=service_base_url,
        path="/v1/internal/iron-report/data",
        method="POST",
        input_model=IronMarketDataInput,
        timeout_seconds=15,
        max_response_chars=180_000,
    )
    registry.register_http_tool(
        tool_name="iron_report_research",
        provider="iron_report_http",
        display_name="Iron Ore Safe Market Research",
        description=(
            "Search attributable iron ore market context for one report date. "
            "This tool always returns a stable structure and automatically returns the fixed "
            "news snapshot with research_status FALLBACK when live search is unavailable or empty."
        ),
        base_url=service_base_url,
        path="/v1/internal/iron-report/research",
        method="POST",
        input_model=IronReportResearchInput,
        timeout_seconds=15,
        max_response_chars=80_000,
    )
    registry.register_skill_package(
        package_name="iron-report-package",
        display_name="Iron Ore Daily and Weekly Report",
        description="Generate source-grounded iron ore market reports with code-created charts.",
        tags=["iron-ore", "market", "daily", "weekly", "report"],
        primary_skill="iron-report-generator",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="iron-report-agent",
        name="Iron Ore Report Agent",
        description="Analyzes validated iron ore data, searches market context, and generates dynamic charts.",
        agent_type="single",
        model_id=model_id,
        system_prompt=(
            "你是铁矿石市场日报和周报 Agent，必须严格遵循当前 Skill。"
            "只使用工具返回的市场数据以及可归因的实时检索或固定快照来源。先调用市场数据工具；"
            "需要研究时只能调用 iron_report_research，不能直接调用 web_search；"
            "使用 code_interpreter 完成数值分析并动态生成两张 PNG 图表；始终区分 report_date 和 data_as_of_date；"
            "不得编造缺失的基本面数据。所有面向读者的报告内容必须使用简体中文，包括标题、摘要、指标名称、"
            "章节标题与正文、发现、证据、风险、限制以及图表标题、坐标轴、图例和注释。"
            "JSON 属性名、枚举、标识符、URL、市场代码及必要的官方来源名称保持原样；"
            "单位优先使用中文可读形式，例如元/吨、美元/吨、手和%。"
            "最终只返回一个有效 JSON 对象，不得使用 Markdown 包裹。"
            "code_interpreter 只能用于计算和生成两张图，不能自行发明另一套报告 JSON。"
            "最终对象必须通过以下精确 JSON Schema，且不能包含额外属性："
            f"{output_schema}"
        ),
        default_tools=["iron_market_data", "iron_report_research", "code_interpreter"],
        default_datasets=[],
    )
    registry.register_task(
        task_type="iron.report.generate",
        name="Iron Ore Daily or Weekly Report",
        description="Generate one real-data iron ore report with dynamic charts and attributable evidence.",
        handler="scheduler",
        default_agent_id="iron-report-agent",
        default_skill_package="iron-report-package",
        default_primary_skill="iron-report-generator",
        default_tools=["iron_market_data", "iron_report_research", "code_interpreter"],
        default_datasets=[],
        input_model=IronReportTaskPayload,
        output_model=IronReportTaskOutput,
        result_sink_url=(
            f"{service_base_url}/v1/internal/iron-report/task-result?callback_token={callback_token}"
        ),
    )
