"""Travel-specific recognition of files already owned by Framework attachments."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .attendance_recognition import (
    AttendanceRecognitionResponse,
    _ocr_structure,
    parse_attendance_structure,
)
from .invoice_recognition import (
    MAX_FILE_BYTES,
    MAX_FILES,
    InvoiceRecognitionResponse,
    _check_token,
    parse_invoice,
)


class InvoiceFilesRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=MAX_FILES)


class AttendanceFileRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=128)


async def _ready_pdf(file_service, file_id: str, tenant_id: str):
    entity = await file_service.get_file(file_id=file_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(404, "附件不存在或不属于当前租户")
    if entity.status != "succeeded":
        raise HTTPException(409, "附件仍在解析或解析失败")
    if not (entity.file_name or "").lower().endswith(".pdf"):
        raise HTTPException(415, "当前仅支持 PDF 发票或签到表")
    if not entity.file_size or entity.file_size > MAX_FILE_BYTES:
        raise HTTPException(413, "文件为空或超过 20MB")
    return entity


async def recognize_invoice_files(file_service, file_ids: list[str], tenant_id: str):
    file_ids = list(dict.fromkeys(file_ids))
    invoices = []
    for file_id in file_ids:
        entity = await _ready_pdf(file_service, file_id, tenant_id)
        result = await file_service.get_text_slice(
            file_id=file_id, tenant_id=tenant_id, offset=0, limit=500_000
        )
        if not result or not result["content"].strip():
            raise HTTPException(422, f"{entity.file_name} 未解析出可识别文字")
        if result["has_more"] or result["truncated_at_extract"]:
            raise HTTPException(422, f"{entity.file_name} 内容未完整解析，不能自动核算金额")
        invoices.append(parse_invoice(
            entity.file_name, result["content"], "PLATFORM_PARSE", file_id
        ))

    def summed(field: str) -> Decimal:
        return sum((getattr(item, field) or Decimal("0") for item in invoices), Decimal("0"))

    return InvoiceRecognitionResponse(
        invoices=invoices,
        total_amount=summed("total_amount"),
        total_amount_excluding_tax=summed("amount_excluding_tax"),
        total_tax_amount=summed("tax_amount"),
        file_count=len(invoices),
        recognized_count=sum(not item.warnings for item in invoices),
    )


async def recognize_attendance_file(file_service, file_id: str, tenant_id: str):
    entity = await _ready_pdf(file_service, file_id, tenant_id)
    # OCR text does not establish which participant actually signed. Only a
    # structure result with a populated signature cell may pass verification.
    if not os.getenv("INVOICE_OCR_BASE_URL", "").strip():
        raise HTTPException(422, "签到表需要表格结构识别服务以核对本人签字，当前尚未配置")
    stream = await file_service.read_bytes(file_id=file_id, tenant_id=tenant_id)
    if stream is None:
        raise HTTPException(404, "签到表原件不可读取")
    with stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if not content or len(content) > MAX_FILE_BYTES:
        raise HTTPException(413, "签到表为空或超过 20MB")
    structure = await _ocr_structure(entity.file_name, content)
    return parse_attendance_structure(entity.file_name, structure, "PADDLE_OCR")


def create_travel_file_router() -> APIRouter:
    from backend.attachments.support import get_file_resource_service

    router = APIRouter(prefix="/v1/internal/travel-files")

    @router.post("/invoices:recognize", response_model=InvoiceRecognitionResponse,
                 include_in_schema=False)
    async def invoices(
        request: InvoiceFilesRequest,
        token: Annotated[str, Header(alias="X-Internal-Token")],
        tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
        file_service=Depends(get_file_resource_service),
    ):
        _check_token(token)
        return await recognize_invoice_files(file_service, request.file_ids, tenant_id)

    @router.post("/attendance-sheet:recognize", response_model=AttendanceRecognitionResponse,
                 include_in_schema=False)
    async def attendance(
        request: AttendanceFileRequest,
        token: Annotated[str, Header(alias="X-Internal-Token")],
        tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
        file_service=Depends(get_file_resource_service),
    ):
        _check_token(token)
        return await recognize_attendance_file(file_service, request.file_id, tenant_id)

    return router
