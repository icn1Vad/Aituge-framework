"""Typed attendance-sheet recognition for meeting-related reimbursements."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Annotated, Any, Mapping

import httpx
from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from backend.invoice_recognition import (
    MAX_FILE_BYTES,
    MIN_NATIVE_TEXT_LENGTH,
    _check_token,
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


@dataclass(frozen=True, slots=True)
class _TableToken:
    page_number: int
    text: str
    bbox: tuple[float, float, float, float]
    score: float
    tile_index: int | None = None

    @property
    def x_center(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def width(self) -> float:
        return max(1.0, self.bbox[2] - self.bbox[0])

    @property
    def y_center(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.bbox[3] - self.bbox[1])


async def _ocr_structure(file_name: str, content: bytes) -> Mapping[str, Any]:
    base_url = os.getenv("INVOICE_OCR_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=422, detail="OCR service is unavailable.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240.0)) as client:
            response = await client.post(
                f"{base_url}/v1/internal/ocr/table-structure",
                files={"file": (file_name, content, "application/pdf")},
            )
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("OCR structure payload must be an object")
        return payload
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Attendance OCR failed.") from exc


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _table_tokens(structure: Mapping[str, Any]) -> list[_TableToken]:
    tokens: list[_TableToken] = []
    pages = structure.get("pages")
    if not isinstance(pages, list):
        return tokens
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        try:
            page_number = int(page.get("page_number") or 1)
        except (TypeError, ValueError):
            page_number = 1
        tables = page.get("tables")
        if not isinstance(tables, list):
            continue
        for table in tables:
            if not isinstance(table, Mapping):
                continue
            try:
                tile_index = int(table["tile_index"]) if table.get("tile_index") is not None else None
            except (TypeError, ValueError):
                tile_index = None
            prediction = table.get("table_ocr_pred")
            if not isinstance(prediction, Mapping):
                continue
            texts = prediction.get("rec_texts")
            boxes = prediction.get("rec_boxes")
            scores = prediction.get("rec_scores")
            if not isinstance(texts, list) or not isinstance(boxes, list):
                continue
            score_values = scores if isinstance(scores, list) else []
            for index, (raw_text, raw_box) in enumerate(zip(texts, boxes)):
                text = re.sub(r"\s+", " ", str(raw_text or "")).strip()
                box = _bbox(raw_box)
                if not text or box is None:
                    continue
                try:
                    score = float(score_values[index]) if index < len(score_values) else 0.0
                except (TypeError, ValueError):
                    score = 0.0
                tokens.append(_TableToken(page_number, text, box, score, tile_index))
    return _deduplicate_tokens(tokens)


def _same_physical_token(existing: _TableToken, token: _TableToken) -> bool:
    if existing.page_number != token.page_number or existing.text != token.text:
        return False
    cross_tile = (
        existing.tile_index is not None
        and token.tile_index is not None
        and existing.tile_index != token.tile_index
    )
    if cross_tile:
        # Paddle can shift a token in adjacent overlapping tiles by almost one
        # glyph height. The cap remains below normal table-row spacing.
        x_tolerance = max(18.0, min(80.0, max(existing.width, token.width) * 0.20))
        y_tolerance = max(18.0, min(96.0, max(existing.height, token.height) * 1.10))
    else:
        # Never use the relaxed overlap tolerance inside one tile: equal values
        # in adjacent physical rows must remain separate.
        x_tolerance = max(12.0, token.height * 0.45)
        y_tolerance = max(12.0, token.height * 0.45)
    return (
        abs(existing.x_center - token.x_center) <= x_tolerance
        and abs(existing.y_center - token.y_center) <= y_tolerance
    )


def _deduplicate_tokens(tokens: list[_TableToken]) -> list[_TableToken]:
    result: list[_TableToken] = []
    for token in sorted(tokens, key=lambda item: (item.page_number, item.y_center, item.x_center, -item.score)):
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(result)
                if _same_physical_token(existing, token)
            ),
            None,
        )
        if duplicate_index is None:
            result.append(token)
        elif token.score > result[duplicate_index].score:
            result[duplicate_index] = token
    return result


def _header_token(tokens: list[_TableToken], labels: tuple[str, ...]) -> _TableToken | None:
    matches = [
        token
        for token in tokens
        if any(label in re.sub(r"\s+", "", token.text) for label in labels)
    ]
    return min(matches, key=lambda item: (item.page_number, item.y_center)) if matches else None


def _number_markers(
    tokens: list[_TableToken],
    sequence_header: _TableToken | None,
    participant_header: _TableToken | None,
    header_y: float,
) -> list[_TableToken]:
    candidates = [
        token
        for token in tokens
        if token.y_center > header_y
        and re.fullmatch(r"\d{1,3}", token.text)
        and 0 < int(token.text) < 1000
    ]
    if participant_header is not None:
        candidates = [token for token in candidates if token.x_center < participant_header.x_center]
    if sequence_header is not None:
        tolerance = (
            max(100.0, abs(participant_header.x_center - sequence_header.x_center) * 0.55)
            if participant_header is not None
            else 160.0
        )
        candidates = [
            token for token in candidates
            if abs(token.x_center - sequence_header.x_center) <= tolerance
        ]
    if not candidates:
        return []
    if sequence_header is None:
        clusters: list[list[_TableToken]] = []
        for token in sorted(candidates, key=lambda item: item.x_center):
            cluster = next(
                (
                    value for value in clusters
                    if abs(sum(item.x_center for item in value) / len(value) - token.x_center) <= 80
                ),
                None,
            )
            if cluster is None:
                clusters.append([token])
            else:
                cluster.append(token)
        candidates = min(
            clusters,
            key=lambda value: (-len(value), sum(item.x_center for item in value) / len(value)),
        )
    return sorted(candidates, key=lambda item: (item.page_number, item.y_center))


def _column_anchors(
    markers: list[_TableToken],
    participant_header: _TableToken,
    organization_header: _TableToken,
    signature_header: _TableToken,
) -> dict[str, float]:
    return {
        "sequence": sum(token.x_center for token in markers) / len(markers),
        "participant": participant_header.x_center,
        "organization": organization_header.x_center,
        "signature": signature_header.x_center,
    }


def _column_name(x_center: float, anchors: Mapping[str, float]) -> str:
    return min(anchors, key=lambda name: abs(anchors[name] - x_center))


def _row_tokens(
    tokens: list[_TableToken],
    markers: list[_TableToken],
    anchors: Mapping[str, float],
    header_y: float,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    markers_by_page: dict[int, list[_TableToken]] = {}
    for marker in markers:
        markers_by_page.setdefault(marker.page_number, []).append(marker)
    for page_number, page_markers in sorted(markers_by_page.items()):
        ordered = sorted(page_markers, key=lambda item: item.y_center)
        page_tokens = [
            token for token in tokens
            if token.page_number == page_number and token.y_center > header_y
        ]
        for index, marker in enumerate(ordered):
            previous_center = ordered[index - 1].y_center if index else header_y
            next_center = (
                ordered[index + 1].y_center
                if index + 1 < len(ordered)
                else marker.y_center + max(marker.y_center - previous_center, marker.height * 2)
            )
            top = (previous_center + marker.y_center) / 2
            bottom = (marker.y_center + next_center) / 2
            columns: dict[str, list[_TableToken]] = {
                "participant": [],
                "organization": [],
                "signature": [],
            }
            for token in page_tokens:
                if token is marker or not top <= token.y_center < bottom:
                    continue
                column = _column_name(token.x_center, anchors)
                if column in columns:
                    columns[column].append(token)
            row = {"sequence": marker.text}
            for name, values in columns.items():
                row[name] = " ".join(
                    token.text for token in sorted(values, key=lambda item: item.x_center)
                ).strip()
            rows.append(row)
    return rows


def _structure_text(structure: Mapping[str, Any], tokens: list[_TableToken]) -> str:
    values: list[str] = []
    pages = structure.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, Mapping):
                continue
            blocks = page.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if isinstance(block, Mapping) and block.get("text"):
                    values.append(str(block["text"]))
    values.extend(token.text for token in tokens)
    return "\n".join(values)


def _meeting_title(structure: Mapping[str, Any], header_y: float) -> str | None:
    candidates: list[tuple[int, float, str]] = []
    pages = structure.get("pages")
    if not isinstance(pages, list):
        return None
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        page_number = int(page.get("page_number") or 1)
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            text = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
            box = _bbox(block.get("bbox"))
            if (
                text
                and box is not None
                and page_number == 1
                and (box[1] + box[3]) / 2 < header_y
                and text not in {"签到", "签到表"}
            ):
                candidates.append((page_number, box[1], text))
    if not candidates:
        return None
    candidates.sort()
    preferred = [
        text for _, _, text in candidates
        if any(marker in text for marker in ("会议", "论坛", "活动", "研讨", "大会"))
    ]
    return (preferred or [candidates[0][2]])[0]


def _names_from_cell(value: str) -> list[str]:
    names: list[str] = []
    for candidate in re.split(r"[\s、,，;；/]+", value):
        candidate = candidate.strip("：:")
        if (
            _CHINESE_NAME.fullmatch(candidate)
            and candidate not in _NON_NAME_VALUES
            and not any(marker in candidate for marker in _ORGANIZATION_MARKERS)
        ):
            names.append(candidate)
    return names


def parse_attendance_structure(
    file_name: str,
    structure: Mapping[str, Any],
    method: str = "PADDLE_OCR",
) -> AttendanceRecognitionResponse:
    tokens = _table_tokens(structure)
    plain_text = _structure_text(structure, tokens)
    sequence_header = _header_token(tokens, ("序号", "编号"))
    participant_header = _header_token(tokens, ("参会人", "参会人员", "姓名"))
    organization_header = _header_token(tokens, ("单位名称", "单位"))
    signature_header = _header_token(tokens, ("签到", "签名"))
    required_headers = (participant_header, organization_header, signature_header)
    if not tokens or any(header is None for header in required_headers):
        return parse_attendance_sheet(file_name, plain_text, method)

    assert participant_header is not None
    assert organization_header is not None
    assert signature_header is not None
    header_y = max(
        header.y_center
        for header in (sequence_header, participant_header, organization_header, signature_header)
        if header is not None
    )
    markers = _number_markers(tokens, sequence_header, participant_header, header_y)
    if not markers:
        return parse_attendance_sheet(file_name, plain_text, method)
    anchors = _column_anchors(
        markers,
        participant_header,
        organization_header,
        signature_header,
    )
    rows = _row_tokens(tokens, markers, anchors, header_y)
    participant_names = _unique([
        name for row in rows for name in _names_from_cell(row["participant"])
    ])
    organizations = _unique([
        row["organization"] for row in rows if row["organization"]
    ])
    signature_count = sum(1 for row in rows if row["signature"])
    compact = re.sub(r"\s+", "", plain_text)
    is_sheet = "签到" in compact and ("参会" in compact or "单位" in compact)
    warnings: list[str] = []
    if not is_sheet:
        warnings.append("NOT_ATTENDANCE_SHEET")
    if not rows:
        warnings.append("NO_NUMBERED_ROWS")
    if not signature_count:
        warnings.append("NO_SIGNATURE_EVIDENCE")
    confidence = round(
        (0.45 if is_sheet else 0)
        + (0.25 if rows else 0)
        + (0.30 if signature_count else 0),
        2,
    )
    return AttendanceRecognitionResponse(
        file_name=file_name,
        recognition_method=method,
        is_attendance_sheet=is_sheet,
        meeting_title=_meeting_title(structure, header_y),
        numbered_row_count=len(rows),
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
        if len(re.sub(r"\s+", "", text)) >= MIN_NATIVE_TEXT_LENGTH:
            return parse_attendance_sheet(file_name, text, "PDF_TEXT")
        structure = await _ocr_structure(file_name, content)
        return parse_attendance_structure(file_name, structure, "PADDLE_OCR")

    return router
