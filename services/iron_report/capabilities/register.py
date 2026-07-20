"""Declare iron ore report capabilities without modifying Framework core."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from iron_report.schemas import IronReportTaskOutput, IronReportTaskPayload, ReportType


CAPABILITY_ID = "iron-report"
CAPABILITY_DIR = Path(__file__).resolve().parent


class IronMarketDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: ReportType
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
            "You are the iron ore daily and weekly report Agent. Follow the active Skill exactly. "
            "Use only supplied market data and attributable search/fallback sources. Call the market data tool first, "
            "use code_interpreter to create the required charts, preserve reportDate and dataAsOfDate separately, "
            "never invent missing fundamentals, and return exactly one valid JSON object. "
            "Use code_interpreter only for numeric analysis and the two PNG charts; do not use it to invent a different "
            "report JSON contract. The final object must validate against this exact JSON Schema, with no extra keys: "
            f"{output_schema}"
        ),
        default_tools=["iron_market_data", "web_search", "code_interpreter"],
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
        default_tools=["iron_market_data", "web_search", "code_interpreter"],
        default_datasets=[],
        input_model=IronReportTaskPayload,
        output_model=IronReportTaskOutput,
        result_sink_url=(
            f"{service_base_url}/v1/internal/iron-report/task-result?callback_token={callback_token}"
        ),
    )
