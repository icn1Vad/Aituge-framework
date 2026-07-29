"""Render a bounded Proof analytical report as a safe HTML artifact."""

from __future__ import annotations

import html
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from tool.artifacts import ArtifactPublisher


MAX_REPORT_SPEC_CHARS = 16_000


class HtmlReportMetric(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=200)


class HtmlReportSection(BaseModel):
    heading: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=2_000)
    bullets: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("bullets")
    @classmethod
    def validate_bullets(cls, values: list[str]) -> list[str]:
        return [_bounded_text(value, 400, "section bullet") for value in values]


class HtmlReportTable(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    headers: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=300)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, values: list[str]) -> list[str]:
        return [_bounded_text(value, 80, "table header") for value in values]

    @model_validator(mode="after")
    def validate_rows(self) -> "HtmlReportTable":
        width = len(self.headers)
        normalized: list[list[str]] = []
        for row in self.rows:
            if len(row) != width:
                raise ValueError(
                    f"table row has {len(row)} cells but {width} headers were declared"
                )
            normalized.append(
                [_bounded_text(value, 300, "table cell") for value in row]
            )
        self.rows = normalized
        return self


class HtmlReportSpec(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)
    metrics: list[HtmlReportMetric] = Field(default_factory=list, max_length=8)
    sections: list[HtmlReportSection] = Field(default_factory=list, max_length=8)
    tables: list[HtmlReportTable] = Field(default_factory=list, max_length=6)
    findings: list[str] = Field(default_factory=list, max_length=10)
    methodology: str = Field(default="", max_length=1_000)
    source_note: str = Field(default="", max_length=500)

    @field_validator("findings")
    @classmethod
    def validate_findings(cls, values: list[str]) -> list[str]:
        return [_bounded_text(value, 500, "finding") for value in values]

    @model_validator(mode="after")
    def validate_total_size(self) -> "HtmlReportSpec":
        payload = self.model_dump(mode="json")
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if size > MAX_REPORT_SPEC_CHARS:
            raise ValueError(
                f"report specification is {size} characters; limit is "
                f"{MAX_REPORT_SPEC_CHARS}. Use aggregates and representative rows."
            )
        return self


@dataclass(slots=True)
class HtmlReportRendererConfig:
    artifact_publisher: ArtifactPublisher | None = None


class HtmlReportRenderer:
    """Create a reader-facing HTML artifact from a compact report specification."""

    def __init__(self, config: HtmlReportRendererConfig | None = None) -> None:
        self.config = config or HtmlReportRendererConfig()

    async def arender(
        self,
        *,
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
        try:
            spec = HtmlReportSpec.model_validate(
                {
                    "title": title,
                    "subtitle": subtitle,
                    "summary": summary,
                    "metrics": metrics or [],
                    "sections": sections or [],
                    "tables": tables or [],
                    "findings": findings or [],
                    "methodology": methodology,
                    "source_note": source_note,
                }
            )
        except ValidationError as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_report_spec",
                    "message": _validation_message(exc),
                    "guidance": (
                        "Keep the report analytical: use aggregates, short narrative "
                        "sections, and representative table rows. Do not send raw HTML, "
                        "Python code, policy text, or a complete policy inventory."
                    ),
                    "artifacts": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        publisher = self.config.artifact_publisher
        if publisher is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "artifact_publisher_unavailable",
                    "message": "The report renderer cannot publish files in this task.",
                    "artifacts": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        document = render_html_report(spec)
        with tempfile.TemporaryDirectory(prefix="tuge-html-report-") as temp_dir:
            path = Path(temp_dir) / "report.html"
            path.write_text(document, encoding="utf-8")
            artifact = await publisher.publish(
                path,
                sequence=1,
                mime="text/html",
            )

        return json.dumps(
            {
                "ok": True,
                "title": spec.title,
                "report_chars": len(document),
                "artifacts": [artifact.to_dict()],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def render_html_report(spec: HtmlReportSpec) -> str:
    """Render only escaped reader content inside a fixed responsive template."""

    metric_cards = "".join(
        f"""
        <article class="metric">
          <div class="metric-label">{_escape(metric.label)}</div>
          <div class="metric-value">{_escape(metric.value)}</div>
          {_optional("div", metric.note, "metric-note")}
        </article>
        """
        for metric in spec.metrics
    )
    section_cards = "".join(_render_section(section) for section in spec.sections)
    table_cards = "".join(_render_table(table) for table in spec.tables)
    findings = ""
    if spec.findings:
        items = "".join(f"<li>{_escape(item)}</li>" for item in spec.findings)
        findings = f"""
        <section class="card">
          <h2>关键发现</h2>
          <ol class="findings">{items}</ol>
        </section>
        """
    methodology = ""
    if spec.methodology or spec.source_note:
        methodology = f"""
        <section class="card methodology">
          <h2>方法与来源</h2>
          {_paragraphs(spec.methodology)}
          {_optional("p", spec.source_note, "source-note")}
        </section>
        """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(spec.title)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#667085; --line:#e4e7ec;
      --surface:#fff; --canvas:#f4f6f8; --brand:#175cd3; --accent:#0e9384; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--canvas); color:var(--ink);
      font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
      "Microsoft YaHei",sans-serif; }}
    header {{ padding:56px max(24px,calc((100vw - 1080px)/2)); color:#fff;
      background:linear-gradient(135deg,#0b1f3a,#175cd3 62%,#0e9384); }}
    header h1 {{ margin:0 0 10px; font-size:clamp(28px,4vw,44px); line-height:1.2; }}
    header p {{ margin:0; max-width:820px; opacity:.88; font-size:17px; }}
    main {{ width:min(1080px,calc(100% - 32px)); margin:28px auto 56px; }}
    .summary,.card,.metric {{ background:var(--surface); border:1px solid var(--line);
      border-radius:16px; box-shadow:0 8px 24px rgba(16,24,40,.05); }}
    .summary,.card {{ padding:24px; margin-bottom:18px; }}
    .summary {{ border-left:5px solid var(--brand); font-size:16px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
      gap:14px; margin:18px 0; }}
    .metric {{ padding:18px; }}
    .metric-label,.metric-note,.source-note {{ color:var(--muted); }}
    .metric-value {{ margin:6px 0 2px; color:var(--brand); font-size:28px; font-weight:750; }}
    h2 {{ margin:0 0 12px; font-size:22px; }}
    p {{ margin:8px 0; }}
    ul,ol {{ margin:10px 0 0; padding-left:22px; }}
    li + li {{ margin-top:6px; }}
    .table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px; }}
    table {{ width:100%; border-collapse:collapse; min-width:560px; }}
    th,td {{ padding:11px 13px; border-bottom:1px solid var(--line); text-align:left;
      vertical-align:top; }}
    th {{ background:#f8fafc; color:#344054; font-weight:650; }}
    tbody tr:last-child td {{ border-bottom:0; }}
    .table-note {{ margin-top:10px; color:var(--muted); font-size:13px; }}
    .methodology {{ border-top:4px solid var(--accent); }}
    footer {{ text-align:center; color:var(--muted); padding:20px; font-size:13px; }}
    @media (max-width:640px) {{
      header {{ padding:36px 20px; }}
      main {{ width:min(100% - 20px,1080px); margin-top:16px; }}
      .summary,.card {{ padding:18px; border-radius:12px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{_escape(spec.title)}</h1>
    {_optional("p", spec.subtitle)}
  </header>
  <main>
    <section class="summary">{_paragraphs(spec.summary)}</section>
    {f'<section class="metrics">{metric_cards}</section>' if metric_cards else ''}
    {section_cards}
    {table_cards}
    {findings}
    {methodology}
  </main>
  <footer>由结构化报告渲染器生成</footer>
</body>
</html>
"""


def _render_section(section: HtmlReportSection) -> str:
    bullets = ""
    if section.bullets:
        items = "".join(f"<li>{_escape(item)}</li>" for item in section.bullets)
        bullets = f"<ul>{items}</ul>"
    return f"""
    <section class="card">
      <h2>{_escape(section.heading)}</h2>
      {_paragraphs(section.body)}
      {bullets}
    </section>
    """


def _render_table(table: HtmlReportTable) -> str:
    headers = "".join(f"<th>{_escape(item)}</th>" for item in table.headers)
    rows = "".join(
        "<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in row) + "</tr>"
        for row in table.rows
    )
    return f"""
    <section class="card">
      <h2>{_escape(table.title)}</h2>
      <div class="table-wrap">
        <table>
          <thead><tr>{headers}</tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      {_optional("p", table.note, "table-note")}
    </section>
    """


def _paragraphs(value: str) -> str:
    return "".join(
        f"<p>{_escape(part.strip())}</p>"
        for part in str(value or "").splitlines()
        if part.strip()
    )


def _optional(tag: str, value: str, class_name: str = "") -> str:
    if not value:
        return ""
    class_attr = f' class="{class_name}"' if class_name else ""
    return f"<{tag}{class_attr}>{_escape(value)}</{tag}>"


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _bounded_text(value: Any, limit: int, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be blank")
    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return text


def _validation_message(exc: ValidationError) -> str:
    details = []
    for item in exc.errors(include_url=False)[:4]:
        path = ".".join(str(part) for part in item.get("loc") or []) or "report"
        details.append(f"{path}: {item.get('msg')}")
    return "; ".join(details)[:1_000]
