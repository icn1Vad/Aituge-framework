"""Service-owned Huatai evidence extraction and review endpoints.

The upload lifecycle remains in Framework attachments; this adapter only
interprets a tenant-scoped, already-parsed file and returns evidence-bound
facts for Java's versioned audit ledger.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .vision import (
    EXTRACTION_PROMPT,
    ModelJsonClient,
    VisionError,
    normalize_extraction,
    review_documents,
)


class ExtractFileRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(AUTO|CONTRACT|INVOICE|ACCEPTANCE|OTHER)$")


async def extract_parsed_file(request: ExtractFileRequest, file_service) -> dict:
    file = await file_service.get_file(file_id=request.file_id, tenant_id=request.tenant_id)
    if file is None:
        raise HTTPException(404, "Attachment not found in tenant")
    if file.status != "succeeded":
        raise HTTPException(409, "Attachment has not finished parsing")
    result = await file_service.get_text_slice(
        file_id=request.file_id, tenant_id=request.tenant_id, offset=0, limit=500_000
    )
    if not result or not result["content"].strip():
        return {
            "kind": "OTHER" if request.kind == "AUTO" else request.kind,
            "status": "NEEDS_REVIEW", "facts": [], "installments": [],
            "evidence": [], "warnings": ["附件未提取到可核对文字，请人工核对原件"],
            "method": "PLATFORM_ATTACHMENT_TEXT", "prompt_version": "huatai-text-1",
        }
    content = result["content"]
    # A bounded model input may support a preliminary extraction, but never a
    # whole-document pass when the parser or model input was truncated.
    model_limit = 80_000
    exposed = content[:model_limit]
    version = sha256(content.encode("utf-8")).hexdigest()
    catalog = []
    for index, start in enumerate(range(0, len(exposed), 8_000), 1):
        chunk = exposed[start:start + 8_000]
        catalog.append({
            "source_id": f"F{version[:16]}C{index}",
            "document_version": version,
            "block_id": str(index),
            "char_start": start,
            "char_end": start + len(chunk),
            "kind": "TEXT_CHUNK",
        })
    chunks = "\n\n".join(
        f"证据编号 {item['source_id']}:\n{exposed[item['char_start']:item['char_end']]}"
        for item in catalog
    )
    prompt = (EXTRACTION_PROMPT.replace("页面编号", "证据编号")
              + "\n本次只提供通用附件服务解析出的文字，不提供图像。"
              + "只引用下列真实证据编号；不要推断签名、盖章或版面信息。"
              + "\n用户标注类别：" + request.kind + "\n" + chunks)
    raw, meta = await ModelJsonClient().complete(model_id=None, prompt=prompt)
    normalized = normalize_extraction(raw, catalog, request.kind)
    partial = result["truncated_at_extract"] or result["has_more"] or len(content) > model_limit
    if partial:
        normalized["status"] = "NEEDS_REVIEW"
        normalized.setdefault("warnings", []).append("文件未被完整送入模型，结果只能作初步参考")
    return {**normalized, **meta, "method": "PLATFORM_ATTACHMENT_TEXT",
            "prompt_version": "huatai-text-1", "partial_input": partial}


def create_workflow_audit_router() -> APIRouter:
    from backend.attachments.support import get_file_resource_service
    from .invoice_recognition import _check_token

    router = APIRouter(prefix="/v1/internal/workflow-audit")

    @router.post("/extract-file", include_in_schema=False)
    async def extract_file(
        request: ExtractFileRequest,
        token: Annotated[str, Header(alias="X-Internal-Token")],
        file_service=Depends(get_file_resource_service),
    ):
        _check_token(token)
        try:
            return await extract_parsed_file(request, file_service)
        except VisionError as exc:
            raise HTTPException(422, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/review", include_in_schema=False)
    async def review(payload: dict, token: Annotated[str, Header(alias="X-Internal-Token")]):
        _check_token(token)
        try:
            return await review_documents(payload)
        except VisionError as exc:
            raise HTTPException(422, detail={"code": exc.code, "message": str(exc)}) from exc

    return router
