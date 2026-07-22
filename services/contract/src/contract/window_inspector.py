"""Test-only window inspector; the production Contract API does not import this module."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
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
    page_number: int | None = None


class ExpectedBlockView(StrictView):
    block_id: str
    text_length: int


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
    context_text: str
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
    document_id: str
    generation_id: str
    block_count: int
    section_count: int
    window_count: int
    warnings: list[str]
    coverage: CoverageView
    expected_blocks: list[ExpectedBlockView]
    sections: list[SectionView]
    windows: list[WindowView]


class PartyContextView(StrictView):
    party_a_name: str
    party_b_name: str
    perspective: str
    contract_type: str = "AUTO"
    review_attitude: str = "NEUTRAL"


class ExtractWindowView(StrictView):
    window: WindowView
    party_context: PartyContextView


class ExtractAllView(StrictView):
    inspection: InspectionView
    party_context: PartyContextView


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
            generation_id = f"window-inspector-{digest[:24]}"
            parsed = NativeContractParser().parse(
                source_path,
                generation_id=generation_id,
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
        document_id=f"window-document-{digest[:24]}",
        generation_id=generation_id,
        block_count=len(parsed.blocks),
        section_count=len(sections),
        window_count=len(windows),
        warnings=list(parsed.warnings),
        coverage=_coverage_view(coverage),
        expected_blocks=[
            ExpectedBlockView(block_id=item.block_id, text_length=len(item.text))
            for item in parsed.blocks
        ],
        sections=[_section_view(item) for item in sections],
        windows=[_window_view(item) for item in windows],
    )


@app.post("/api/extract-window")
async def extract_window(request: ExtractWindowView):
    extractor_url = os.getenv("CONTRACT_WINDOW_EXTRACTOR_URL", "").rstrip("/")
    if not extractor_url:
        raise HTTPException(status_code=503, detail="窗口抽取测试服务尚未配置")
    payload = {
        "window": {
            "window_id": request.window.window_id,
            "source_text": request.window.source_text,
            "context_text": request.window.context_text,
            "offset_map": [item.model_dump() for item in request.window.offset_map],
        },
        "party_context": request.party_context.model_dump(),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            response = await client.post(f"{extractor_url}/api/extract-window", json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"窗口抽取测试服务不可用：{exc}") from exc
    try:
        content = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="窗口抽取测试服务返回了非 JSON 响应") from exc
    return JSONResponse(status_code=response.status_code, content=content)


@app.post("/api/extract-all")
async def extract_all(request: ExtractAllView):
    extractor_url = os.getenv("CONTRACT_WINDOW_EXTRACTOR_URL", "").rstrip("/")
    if not extractor_url:
        raise HTTPException(status_code=503, detail="窗口抽取测试服务尚未配置")
    payload = {
        "pipeline": {
            "document_id": request.inspection.document_id,
            "generation_id": request.inspection.generation_id,
            "expected_blocks": [item.model_dump() for item in request.inspection.expected_blocks],
            "expected_section_ids": [item.section_id for item in request.inspection.sections],
            "windows": [item.model_dump() for item in request.inspection.windows],
            "concurrency": 10,
        },
        "party_context": request.party_context.model_dump(),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0)) as client:
            response = await client.post(f"{extractor_url}/api/extract-all", json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"窗口抽取测试服务不可用：{exc}") from exc
    try:
        content = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="窗口抽取测试服务返回了非 JSON 响应") from exc
    return JSONResponse(status_code=response.status_code, content=content)


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
        context_text=window.context_text,
        offset_map=[OffsetView.model_validate(item, from_attributes=True) for item in window.offset_map],
    )
