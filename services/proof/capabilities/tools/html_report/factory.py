"""Factory for the task-owned structured HTML report renderer."""

from __future__ import annotations

from typing import Any

from llama_index.core.tools.function_tool import FunctionTool

from tool.bundle import ToolBundle
from tool.registry.config import ToolProviderConfig

from .renderer import HtmlReportRenderer, HtmlReportRendererConfig, HtmlReportSpec


def create_html_report_renderer_tools(
    config: HtmlReportRendererConfig | None = None,
) -> list[FunctionTool]:
    renderer = HtmlReportRenderer(config)

    async def arender_html_report(
        title: str,
        summary: str,
        subtitle: str = "",
        metrics: list[dict[str, Any]] | None = None,
        sections: list[dict[str, Any]] | None = None,
        tables: list[dict[str, Any]] | None = None,
        findings: list[str] | None = None,
        methodology: str = "",
        source_note: str = "",
    ) -> str:
        return await renderer.arender(
            title=title,
            subtitle=subtitle,
            summary=summary,
            metrics=metrics,
            sections=sections,
            tables=tables,
            findings=findings,
            methodology=methodology,
            source_note=source_note,
        )

    return [
        FunctionTool.from_defaults(
            async_fn=arender_html_report,
            fn_schema=HtmlReportSpec,
            name="RenderHtmlReport",
            description="""Render a compact structured analytical report as a published HTML artifact.

Use this tool when the user asks for an HTML research report, overview, or reader-facing analytical report.
Provide only report content: title, summary, compact metrics, narrative sections, bounded tables, findings, methodology, and source notes.
Do not provide Python, raw HTML, policy clause text, or a complete policy inventory. The renderer escapes content, applies the report template, and publishes the HTML file automatically.
The result contains the artifact reference; do not rewrite or invent its URL.""",
            return_direct=False,
        )
    ]


def create_html_report_renderer_bundle(
    config: HtmlReportRendererConfig | None = None,
) -> ToolBundle:
    return ToolBundle.from_tools(create_html_report_renderer_tools(config))


def create_capability_html_report_bundle(
    config: ToolProviderConfig,
) -> ToolBundle:
    """Build the Proof tool with task-scoped framework dependencies."""

    renderer_config = HtmlReportRendererConfig(
        artifact_publisher=config.config.get("artifact_publisher"),
    )
    return create_html_report_renderer_bundle(renderer_config)
