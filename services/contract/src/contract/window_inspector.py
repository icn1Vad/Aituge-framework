"""Test-only window inspector; the production Contract API does not import this module."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from contract.errors import ContractError
from contract.ir.windowing import (
    CoverageReport,
    SectionUnit,
    SectionWindow,
    WindowBuildError,
    build_section_units,
    build_section_windows,
    validate_window_coverage,
)
from contract.parser.native import NativeContractParser


_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_STATIC_FILE = Path(__file__).with_name("window-inspector.html")

app = FastAPI(title="Contract IR Window Inspector", version="1.0", docs_url=None, redoc_url=None)


class StrictView(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OffsetView(StrictView):
    rendered_start: int
    rendered_end: int
    block_id: str
    block_no: int
    block_char_start: int
    block_char_end: int


class SectionView(StrictView):
    section_id: str
    sequence_no: int
    section_type: str
    title: str
    clause_no: str | None
    parent_section_id: str | None
    major_section_id: str | None
    block_ids: list[str]
    subclause_block_ids: list[str]


class WindowView(StrictView):
    window_id: str
    sequence_no: int
    section_ids: list[str]
    heading_path: list[str]
    clause_nos: list[str]
    primary_block_ids: list[str]
    estimated_tokens: int
    source_text: str
    offset_map: list[OffsetView]


class CoverageView(StrictView):
    valid: bool
    expected_block_count: int
    covered_block_count: int
    split_block_ids: list[str]
    missing_block_ids: list[str]
    overlap_block_ids: list[str]


class InspectionView(StrictView):
    filename: str
    file_type: str
    content_sha256: str
    block_count: int
    section_count: int
    window_count: int
    warnings: list[str]
    coverage: CoverageView
    sections: list[SectionView]
    windows: list[WindowView]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse(_STATIC_FILE.read_text(encoding="utf-8"))


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "UP"}


@app.post("/api/inspect", response_model=InspectionView)
async def inspect_contract(file: UploadFile = File(...)) -> InspectionView:
    filename = Path(file.filename or "contract").name
    suffix = Path(filename).suffix.lower()
    if suffix not in NativeContractParser.supported_extensions:
        raise HTTPException(status_code=415, detail="测试页面仅接受 PDF 或 DOCX")

    payload = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="测试文件不能超过 30 MB")
    if not payload:
        raise HTTPException(status_code=422, detail="测试文件不能为空")

    digest = hashlib.sha256(payload).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="contract-window-") as directory:
            source_path = Path(directory) / f"source{suffix}"
            source_path.write_bytes(payload)
            parsed = NativeContractParser().parse(
                source_path,
                generation_id=f"window-inspector-{digest[:24]}",
            )
        sections = build_section_units(parsed.blocks)
        windows = build_section_windows(sections)
        coverage = validate_window_coverage(parsed.blocks, windows)
    except ContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except WindowBuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return InspectionView(
        filename=filename,
        file_type=parsed.file_type,
        content_sha256=f"sha256:{digest}",
        block_count=len(parsed.blocks),
        section_count=len(sections),
        window_count=len(windows),
        warnings=list(parsed.warnings),
        coverage=_coverage_view(coverage),
        sections=[_section_view(item) for item in sections],
        windows=[_window_view(item) for item in windows],
    )


def _coverage_view(report: CoverageReport) -> CoverageView:
    return CoverageView(
        valid=report.valid,
        expected_block_count=report.expected_block_count,
        covered_block_count=report.covered_block_count,
        split_block_ids=list(report.split_block_ids),
        missing_block_ids=list(report.missing_block_ids),
        overlap_block_ids=list(report.overlap_block_ids),
    )


def _section_view(section: SectionUnit) -> SectionView:
    return SectionView(
        section_id=section.section_id,
        sequence_no=section.sequence_no,
        section_type=section.section_type,
        title=section.title,
        clause_no=section.clause_no,
        parent_section_id=section.parent_section_id,
        major_section_id=section.major_section_id,
        block_ids=list(section.block_ids),
        subclause_block_ids=list(section.subclause_block_ids),
    )


def _window_view(window: SectionWindow) -> WindowView:
    return WindowView(
        window_id=window.window_id,
        sequence_no=window.sequence_no,
        section_ids=list(window.section_ids),
        heading_path=list(window.heading_path),
        clause_nos=list(window.clause_nos),
        primary_block_ids=list(window.primary_block_ids),
        estimated_tokens=window.estimated_tokens,
        source_text=window.source_text,
        offset_map=[OffsetView.model_validate(item, from_attributes=True) for item in window.offset_map],
    )
