from __future__ import annotations

import hmac
import os
from typing import Annotated

from aituge_model_config import get_model_pack_for_ai_mode
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from service.conversation.llm_runner import LlmRuntime
from service.conversation.title_generator import ConversationTitleGenerator


class ChatTitleGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)


class ChatTitleGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


def create_chat_title_llm_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/internal/chat-titles:generate",
        response_model=ChatTitleGenerationResponse,
        include_in_schema=False,
    )
    async def generate_chat_title(
        payload: ChatTitleGenerationRequest,
        internal_token: Annotated[str, Header(alias="X-Internal-Token")],
        tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
        user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
        request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
        ai_mode: Annotated[str, Header(alias="X-AI-Mode")] = "public",
    ) -> ChatTitleGenerationResponse:
        expected = (
            os.getenv("FRAMEWORK_INTERNAL_TOKEN", "").strip()
            or os.getenv("CONTRACT_INTERNAL_TOKEN", "").strip()
        )
        if not expected or not hmac.compare_digest(internal_token, expected):
            raise HTTPException(status_code=401, detail="Invalid internal credential")

        normalized_tenant_id = str(tenant_id or "").strip()
        if not normalized_tenant_id or normalized_tenant_id.lower() in {
            "null",
            "none",
            "undefined",
        }:
            raise HTTPException(status_code=400, detail="X-Tenant-Id is required.")

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id or normalized_user_id.lower() in {
            "null",
            "none",
            "undefined",
        }:
            raise HTTPException(status_code=400, detail="X-User-Id is required.")

        try:
            model_pack = get_model_pack_for_ai_mode(ai_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        runtime = LlmRuntime(
            tenant_id=normalized_tenant_id,
            model_pack_id=model_pack.id,
        )
        try:
            title = await ConversationTitleGenerator(runtime).generate(
                payload.question,
                trace_id=request_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return ChatTitleGenerationResponse(title=title)

    return router
