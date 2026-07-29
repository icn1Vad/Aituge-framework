import asyncio
import json
from pathlib import Path

from tool.artifacts import ArtifactRef
from services.proof.capabilities.tools.html_report.factory import (
    create_html_report_renderer_bundle,
    create_html_report_renderer_tools,
)
from services.proof.capabilities.tools.html_report.renderer import (
    HtmlReportRenderer,
    HtmlReportRendererConfig,
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.content = ""
        self.calls: list[tuple[str, int, str]] = []

    async def publish(
        self,
        source_path: Path,
        *,
        sequence: int,
        mime: str,
    ) -> ArtifactRef:
        self.content = source_path.read_text(encoding="utf-8")
        self.calls.append((source_path.name, sequence, mime))
        return ArtifactRef(
            id="report-1",
            name="artifact-001.html",
            mime=mime,
            url="/task-manager/artifacts/report-1/content",
        )


def test_html_report_renderer_publishes_escaped_structured_report():
    async def run():
        publisher = RecordingPublisher()
        renderer = HtmlReportRenderer(
            HtmlReportRendererConfig(artifact_publisher=publisher)
        )
        result = await renderer.arender(
            title="制度报告 <script>alert(1)</script>",
            subtitle="整体概览",
            summary="覆盖制度层级与分类。",
            metrics=[{"label": "制度总数", "value": "84", "note": "现行有效"}],
            sections=[
                {
                    "heading": "层级结构",
                    "body": "三级制度体系。",
                    "bullets": ["一级制度 1 份", "二级制度 45 份"],
                }
            ],
            tables=[
                {
                    "title": "层级分布",
                    "headers": ["层级", "数量"],
                    "rows": [["一级", "1"], ["二级", "45"]],
                    "note": "来源：proof_sql_policy_v",
                }
            ],
            findings=["一级制度数量较少。"],
            methodology="按有效制度聚合。",
            source_note="结构化视图：proof_sql_policy_v",
        )

        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["artifacts"][0]["id"] == "report-1"
        assert publisher.calls == [("report.html", 1, "text/html")]
        assert "<script>" not in publisher.content
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in publisher.content
        assert "一级制度数量较少。" in publisher.content

    asyncio.run(run())


def test_html_report_renderer_rejects_oversized_table_without_throwing():
    async def run():
        publisher = RecordingPublisher()
        renderer = HtmlReportRenderer(
            HtmlReportRendererConfig(artifact_publisher=publisher)
        )
        result = await renderer.arender(
            title="制度报告",
            summary="整体概览",
            tables=[
                {
                    "title": "完整清单",
                    "headers": ["制度"],
                    "rows": [[f"制度 {index}"] for index in range(21)],
                }
            ],
        )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"] == "invalid_report_spec"
        assert "representative table rows" in payload["guidance"]
        assert publisher.calls == []

    asyncio.run(run())


def test_html_report_renderer_tool_contract_is_structured():
    tools = create_html_report_renderer_tools()
    bundle = create_html_report_renderer_bundle()

    assert [tool.metadata.name for tool in tools] == ["RenderHtmlReport"]
    assert [tool.metadata.name for tool in bundle.tools] == ["RenderHtmlReport"]
    schema = tools[0].metadata.fn_schema.model_json_schema()
    assert schema["properties"]["title"]["maxLength"] == 120
    assert schema["properties"]["tables"]["maxItems"] == 6
    assert "raw HTML" in tools[0].metadata.description
