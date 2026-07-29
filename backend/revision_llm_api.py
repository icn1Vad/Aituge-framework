from __future__ import annotations

import hmac
import os
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from service.conversation.llm_runner import LlmRuntime


REVISION_SYSTEM_PROMPT = (
    "你是合同条款修订器。风险已经由上游正式确认；你只生成可直接替换原条款的中文文案。"
    "严格返回JSON对象，不使用工具，不输出解释性前后缀。"
)


class RevisionCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(pattern="^GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY$")
    model_id: str = Field(min_length=1, max_length=160)
    user_prompt: str = Field(min_length=1, max_length=100_000)


class RevisionCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    model_duration_ms: int
    trace_id: str
    provider_request_id: str | None
    finish_reason: str | None


def create_revision_llm_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/internal/contract-revision-drafts:complete",
        response_model=RevisionCompletionResponse,
        include_in_schema=False,
    )
    async def complete_revision_drafts(
        payload: RevisionCompletionRequest,
        internal_token: Annotated[str, Header(alias="X-Internal-Token")],
        tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
        request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ) -> RevisionCompletionResponse:
        expected = os.getenv("CONTRACT_INTERNAL_TOKEN", "")
        if not expected or not hmac.compare_digest(internal_token, expected):
            raise HTTPException(status_code=401, detail="Invalid internal credential")
        tenant_id = str(tenant_id or "").strip()
        if not tenant_id or tenant_id.lower() in {"null", "none", "undefined"}:
            raise HTTPException(status_code=400, detail="X-Tenant-Id is required.")

        completion = await LlmRuntime(tenant_id).complete_with_usage(
            messages=[{"role": "user", "content": payload.user_prompt}],
            model_id=payload.model_id,
            system_prompt=REVISION_SYSTEM_PROMPT,
            max_tokens=4_000,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="revision_draft_mvp",
            trace_id=request_id,
        )
        values: dict[str, Any] = {
            name: getattr(completion, name)
            for name in RevisionCompletionResponse.model_fields
        }
        return RevisionCompletionResponse.model_validate(values)

    return router
