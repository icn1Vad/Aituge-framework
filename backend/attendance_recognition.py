"""Typed attendance-sheet recognition for meeting-related reimbursements."""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from backend.invoice_recognition import (
    MAX_FILE_BYTES,
    MIN_NATIVE_TEXT_LENGTH,
    _check_token,
    _ocr_text,
    _pdf_text,
)

_ROW_NUMBER = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")
_CHINESE_NAME = re.compile(r"^[\u4e00-\u9fff]{2,6}$")
_ORGANIZATION_MARKERS = (
    "有限公司", "股份有限公司", "集团", "公司", "医院", "大学", "学院",
    "银行", "研究院", "中心", "协会", "财险", "保险", "科技", "投资",
)
_NON_NAME_VALUES = {
    "签到", "签到表", "参会人", "参会人员", "单位", "单位名称", "姓名",
    "序号", "签名", "论坛", "活动", "可信赋能", "智安同行",
}


class AttendanceRecognitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: str
    recognition_method: str
    is_attendance_sheet: bool
    meeting_title: str | None = None
    numbered_row_count: int = Field(ge=0)
    signature_evidence_count: int = Field(ge=0)
    participant_names: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def parse_attendance_sheet(
    file_name: str,
    text: str,
    method: str = "PDF_TEXT",
) -> AttendanceRecognitionResponse:
    normalized = text.replace("\u3000", " ").replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in normalized.splitlines()]
    lines = [line for line in lines if line]
    compact = re.sub(r"\s+", "", normalized)
    is_sheet = "签到表" in compact or ("签到" in compact and "参会" in compact)

    row_numbers = set()
    for line in lines:
        row_match = re.match(r"^(\d{1,3})(?:\s|$)", line)
        if row_match and 0 < int(row_match.group(1)) < 1000:
            row_numbers.add(int(row_match.group(1)))
    organizations = _unique([
        line for line in lines
        if len(line) <= 80 and any(marker in line for marker in _ORGANIZATION_MARKERS)
        and "签到" not in line and "论坛" not in line and "活动" not in line
    ])

    participant_names: list[str] = []
    for raw in lines:
        value = re.sub(r"^\d{1,3}\s*|\s*\d{1,3}$", "", raw).strip()
        for candidate in [value, *value.split()]:
            candidate = candidate.strip("：:，,；;")
            if (
                _CHINESE_NAME.fullmatch(candidate)
                and candidate not in _NON_NAME_VALUES
                and not any(marker in candidate for marker in _ORGANIZATION_MARKERS)
            ):
                participant_names.append(candidate)
    participant_names = _unique(participant_names)
    signature_count = min(len(row_numbers), len(participant_names))

    title_candidate = re.sub(r"签到(?:表)?$", "", lines[0]).strip() if lines else ""
    meeting_title = title_candidate or None
    warnings: list[str] = []
    if not is_sheet:
        warnings.append("NOT_ATTENDANCE_SHEET")
    if not row_numbers:
        warnings.append("NO_NUMBERED_ROWS")
    if not signature_count:
        warnings.append("NO_SIGNATURE_EVIDENCE")
    confidence = round(
        (0.45 if is_sheet else 0)
        + (0.25 if row_numbers else 0)
        + (0.30 if signature_count else 0),
        2,
    )
    return AttendanceRecognitionResponse(
        file_name=file_name,
        recognition_method=method,
        is_attendance_sheet=is_sheet,
        meeting_title=meeting_title,
        numbered_row_count=len(row_numbers),
        signature_evidence_count=signature_count,
        participant_names=participant_names[:50],
        organizations=organizations[:50],
        confidence=confidence,
        warnings=warnings,
    )


def create_attendance_recognition_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/internal/attendance-sheet:recognize",
        response_model=AttendanceRecognitionResponse,
        include_in_schema=False,
    )
    async def recognize_attendance_sheet(
        file: Annotated[UploadFile, File(...)],
        internal_token: Annotated[str, Header(alias="X-Internal-Token")],
        tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
        user_id: Annotated[str, Header(alias="X-User-Id")],
    ) -> AttendanceRecognitionResponse:
        _check_token(internal_token)
        if not tenant_id.strip() or not user_id.strip():
            raise HTTPException(status_code=400, detail="Tenant and user are required.")
        file_name = file.filename or "attendance-sheet.pdf"
        if not file_name.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="Only PDF is supported.")
        content = await file.read(MAX_FILE_BYTES + 1)
        if not content or len(content) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Attendance sheet is empty or too large.")
        text = _pdf_text(content)
        method = "PDF_TEXT"
        if len(re.sub(r"\s+", "", text)) < MIN_NATIVE_TEXT_LENGTH:
            text = await _ocr_text(file_name, content)
            method = "PADDLE_OCR"
        return parse_attendance_sheet(file_name, text, method)

    return router
