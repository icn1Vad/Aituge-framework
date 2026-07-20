from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ReportType(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class OutputFormat(StrEnum):
    DOCX = "DOCX"
    PDF = "PDF"


class IronReportCreateRequest(StrictModel):
    report_type: ReportType = Field(alias="reportType")
    as_of_date: date = Field(alias="asOfDate")
    output_formats: list[OutputFormat] = Field(
        default_factory=lambda: [OutputFormat.DOCX, OutputFormat.PDF],
        alias="outputFormats",
        min_length=1,
        max_length=2,
    )
    include_web_research: bool = Field(default=True, alias="includeWebResearch")

    @field_validator("as_of_date", mode="before")
    @classmethod
    def strict_iso_date(cls, value):
        if isinstance(value, datetime):
            raise ValueError("asOfDate 必须是 YYYY-MM-DD 日期，不能包含时间")
        if isinstance(value, str):
            normalized = value.strip()
            try:
                parsed = date.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError("asOfDate 必须是有效的 YYYY-MM-DD 日期") from exc
            if parsed.isoformat() != normalized:
                raise ValueError("asOfDate 必须严格使用 YYYY-MM-DD 格式")
            return parsed
        return value

    @field_validator("output_formats")
    @classmethod
    def unique_output_formats(cls, value: list[OutputFormat]) -> list[OutputFormat]:
        if len(set(value)) != len(value):
            raise ValueError("outputFormats 不能重复")
        return value


class IronReportTaskInput(StrictModel):
    report_type: ReportType
    report_date: date
    data_as_of_date: date
    output_formats: list[OutputFormat]
    include_web_research: bool
    fallback_news_available: bool = True


class IronReportTaskPayload(StrictModel):
    """JSON-safe Task Manager payload contract.

    Framework's shared payload validator persists ``model_dump()`` directly to a
    JSON column. Keep dates as validated ISO strings here, then parse them back
    into ``IronReportTaskInput`` at the service/export boundary.
    """

    report_type: ReportType
    report_date: str
    data_as_of_date: str
    output_formats: list[OutputFormat]
    include_web_research: bool
    fallback_news_available: bool = True

    @field_validator("report_date", "data_as_of_date")
    @classmethod
    def strict_task_date(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = date.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("task date must use YYYY-MM-DD") from exc
        if parsed.isoformat() != normalized:
            raise ValueError("task date must strictly use YYYY-MM-DD")
        return normalized


class ReportMetric(StrictModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    value: float | int | str
    unit: str = Field(default="", max_length=40)
    source_id: str = Field(min_length=1, max_length=120)
    data_as_of_date: date


class ReportSection(StrictModel):
    heading: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    findings: list[str] = Field(default_factory=list, max_length=6)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=6)


class ReportSource(StrictModel):
    source_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(default="", max_length=2000)
    published_at: str = Field(default="", max_length=40)
    source_type: str = Field(default="", max_length=80)


class IronReportTaskOutput(StrictModel):
    report_type: ReportType
    report_date: date
    data_as_of_date: date
    title: str = Field(min_length=1, max_length=300)
    executive_summary: str = Field(min_length=1, max_length=3000)
    metrics: list[ReportMetric] = Field(min_length=1, max_length=30)
    sections: list[ReportSection] = Field(min_length=2, max_length=10)
    sources: list[ReportSource] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    research_status: Literal["LIVE", "FALLBACK", "DISABLED"]
    generated_at: datetime


class IronReportCreated(StrictModel):
    report_id: str
    framework_task_id: str
    framework_run_id: str
    report_type: ReportType
    report_date: date
    data_as_of_date: date
    status: str
    reused: bool


class IronReportArtifact(StrictModel):
    artifact_id: str
    kind: str
    name: str
    mime_type: str
    size: int | None = None
    sha256: str
    content_url: str


class IronReportStatus(StrictModel):
    report_id: str
    framework_task_id: str
    framework_run_id: str | None = None
    report_type: ReportType
    report_date: date
    data_as_of_date: date
    status: str
    progress: int = Field(ge=0, le=100)
    current_stage: str
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    artifacts: list[IronReportArtifact] = Field(default_factory=list)


class SuccessEnvelope(StrictModel):
    success: Literal[True] = True
    data: Any
    request_id: str
