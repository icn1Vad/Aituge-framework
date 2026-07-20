from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

from iron_report.config import Settings
from iron_report.errors import IronReportError
from iron_report.exporter import ExportStateStore, IronReportExporter
from iron_report.schemas import IronReportTaskOutput
from scheduling.scheduler import SchedulingRuntimeOptions


class FakeTasks:
    def __init__(self, task, artifacts=None) -> None:
        self.task = task
        self.artifacts = artifacts or []

    async def get_task(self, task_id):
        return self.task if task_id == self.task.id else None

    async def list_task_artifacts(self, task_id):
        return self.artifacts


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=Path(__file__).resolve().parents[1] / "demo_data",
        runtime_root=tmp_path / "runtime",
        internal_token="test-internal-token",
        model_id="configured-test-model",
        libreoffice_path="/configured/libreoffice",
    )


def _options(tmp_path: Path) -> SchedulingRuntimeOptions:
    return SchedulingRuntimeOptions(
        local_python_artifact_dir=tmp_path / "artifacts",
        local_python_work_dir=tmp_path / "work",
        rag_store=None,
    )


def _output() -> dict:
    return {
        "report_type": "DAILY",
        "report_date": "2026-07-17",
        "data_as_of_date": "2026-07-17",
        "title": "铁矿石市场日报",
        "executive_summary": "当日数据摘要。",
        "metrics": [
            {
                "key": "close",
                "label": "收盘价",
                "value": 762,
                "unit": "CNY/metric_ton",
                "source_id": "sina_iron_ore_i0",
                "data_as_of_date": "2026-07-17",
            }
        ],
        "sections": [
            {"heading": "行情", "summary": "行情分析。", "findings": [], "evidence": [], "risks": []},
            {"heading": "风险", "summary": "风险分析。", "findings": [], "evidence": [], "risks": []},
        ],
        "sources": [
            {
                "source_id": "sina_iron_ore_i0",
                "title": "Prepared I0 series",
                "url": "https://example.test/source",
                "published_at": "2026-07-17",
                "source_type": "market_data",
            }
        ],
        "limitations": ["I0 是连续序列。"],
        "research_status": "FALLBACK",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_missing_chart_artifacts_has_stable_error_and_state(tmp_path: Path) -> None:
    task = SimpleNamespace(
        id="task-1",
        task_type="iron.report.generate",
        current_run_id="run-1",
        input_payload_json={
            "report_type": "DAILY",
            "report_date": "2026-07-17",
            "data_as_of_date": "2026-07-17",
            "output_formats": ["DOCX", "PDF"],
            "include_web_research": True,
            "fallback_news_available": True,
        },
    )
    state = ExportStateStore(tmp_path / "state")
    exporter = IronReportExporter(_settings(tmp_path), _options(tmp_path), state)
    exporter.tasks = FakeTasks(task)

    with pytest.raises(IronReportError) as caught:
        await exporter.accept_task_result(
            {"task_id": "task-1", "run_id": "run-1", "status": "completed", "output": _output()}
        )

    assert caught.value.code == "IRON_REPORT_CHART_ARTIFACT_MISSING"
    assert state.get("task-1")["error_code"] == "IRON_REPORT_CHART_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_invalid_agent_output_has_stable_schema_error(tmp_path: Path) -> None:
    task = SimpleNamespace(
        id="task-1",
        task_type="iron.report.generate",
        current_run_id="run-1",
        input_payload_json={
            "report_type": "DAILY",
            "report_date": "2026-07-17",
            "data_as_of_date": "2026-07-17",
            "output_formats": ["DOCX", "PDF"],
            "include_web_research": True,
            "fallback_news_available": True,
        },
    )
    state = ExportStateStore(tmp_path / "state")
    exporter = IronReportExporter(_settings(tmp_path), _options(tmp_path), state)
    exporter.tasks = FakeTasks(task)

    with pytest.raises(IronReportError) as caught:
        await exporter.accept_task_result(
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "status": "completed",
                "output": {"report_type": "DAILY", "content": "wrong contract"},
            }
        )

    assert caught.value.code == "IRON_REPORT_OUTPUT_SCHEMA_INVALID"
    failure = state.get("task-1")
    assert failure["error_code"] == "IRON_REPORT_OUTPUT_SCHEMA_INVALID"
    assert failure["details"]["validationErrors"]


@pytest.mark.asyncio
async def test_english_narrative_is_rejected_before_archiving(tmp_path: Path) -> None:
    task = SimpleNamespace(
        id="task-english",
        task_type="iron.report.generate",
        current_run_id="run-english",
        input_payload_json={
            "report_type": "DAILY",
            "report_date": "2026-07-17",
            "data_as_of_date": "2026-07-17",
            "output_formats": ["DOCX", "PDF"],
            "include_web_research": False,
            "fallback_news_available": True,
        },
    )
    state = ExportStateStore(tmp_path / "state")
    exporter = IronReportExporter(_settings(tmp_path), _options(tmp_path), state)
    exporter.tasks = FakeTasks(task)
    payload = _output()
    payload["title"] = "Iron Ore Market Daily Report"
    payload["executive_summary"] = "The market closed higher with active trading."

    with pytest.raises(IronReportError) as caught:
        await exporter.accept_task_result(
            {
                "task_id": "task-english",
                "run_id": "run-english",
                "status": "completed",
                "output": payload,
            }
        )

    assert caught.value.code == "IRON_REPORT_OUTPUT_LANGUAGE_INVALID"
    assert caught.value.details["requiredLanguage"] == "zh-CN"
    assert caught.value.details["fields"] == ["title", "executive_summary"]
    assert state.get("task-english")["error_code"] == "IRON_REPORT_OUTPUT_LANGUAGE_INVALID"


def test_libreoffice_is_the_configured_pdf_conversion_path(tmp_path: Path, monkeypatch) -> None:
    exporter = IronReportExporter(
        _settings(tmp_path), _options(tmp_path), ExportStateStore(tmp_path / "state")
    )
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"docx")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        (tmp_path / "report.pdf").write_bytes(b"%PDF-demo")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("iron_report.exporter.subprocess.run", fake_run)

    result = exporter._convert_pdf(docx, tmp_path)

    assert result.read_bytes().startswith(b"%PDF")
    assert commands[0][0] == "/configured/libreoffice"
    assert "--headless" in commands[0]


def test_docx_export_keeps_report_and_data_dates_separate(tmp_path: Path) -> None:
    exporter = IronReportExporter(
        _settings(tmp_path), _options(tmp_path), ExportStateStore(tmp_path / "state")
    )
    chart_one = tmp_path / "chart-one.png"
    chart_two = tmp_path / "chart-two.png"
    # Minimal valid 1x1 PNG files used only by python-docx in the server test image.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cfc000000301010018dd8db10000000049454e44ae426082"
    )
    chart_one.write_bytes(png)
    chart_two.write_bytes(png)
    target = tmp_path / "daily.docx"
    payload = _output()
    payload["report_date"] = "2026-07-12"
    payload["data_as_of_date"] = "2026-07-10"

    exporter._write_docx(target, IronReportTaskOutput.model_validate(payload), [chart_one, chart_two])

    assert target.read_bytes().startswith(b"PK")
    assert target.stat().st_size > 1000

    document = Document(target)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "报告日期：2026-07-12" in text
    assert "数据截至：2026-07-10" in text
    assert "研究方式：固定新闻快照（在线检索降级）" in text
