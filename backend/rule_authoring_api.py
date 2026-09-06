"""Trusted Java-to-Framework authoring API, isolated from contract execution."""
from __future__ import annotations
import asyncio
import hmac
import os
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException
from backend.rule_authoring import AssistRequest, RelatedRequest, assist, rank_related


def trusted_context(
    internal_token: Annotated[str, Header(alias="X-Internal-Token")],
    tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    user_id: Annotated[str, Header(alias="X-User-Id")],
):
    expected = os.getenv("FRAMEWORK_INTERNAL_TOKEN", "").strip() or os.getenv("CONTRACT_INTERNAL_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(expected, internal_token):
        raise HTTPException(401, "Invalid internal credential")
    if any(not value.strip() or value.lower() in {"null", "none", "undefined"} for value in (tenant_id, user_id)):
        raise HTTPException(400, "Trusted tenant and user are required")
    return tenant_id.strip(), user_id.strip()


def create_rule_authoring_router(runtime_factory=None) -> APIRouter:
    router = APIRouter(prefix="/v1/internal/rule-authoring", include_in_schema=False)

    @router.post("/assist")
    async def generate(
        payload: AssistRequest,
        context: Annotated[tuple[str, str], Depends(trusted_context)],
        ai_mode: Annotated[str, Header(alias="X-AI-Mode")] = "public",
        request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    ):
        from backend.rule_authoring_form import explicit_change
        from backend.rule_authoring import validate_answer
        import json
        change = explicit_change(payload.draft, payload.messages[-1].content)
        if change is not None:
            draft, label = change
            try:
                answer = validate_answer(json.dumps({"reply": f"已修改{label}，请确认规则卡片。",
                    "draft": draft, "questions": [], "searchTerms": []}, ensure_ascii=False), payload)
            except ValueError as exc:
                raise HTTPException(422, "字段值无效，请检查日期、分类或依据。") from exc
            return {**answer.model_dump(), "usage": {"promptTokens": 0, "completionTokens": 0}}
        try:
            from aituge_model.config import get_model_pack_for_ai_mode
            pack = get_model_pack_for_ai_mode(ai_mode)
        except ValueError as exc:
            raise HTTPException(400, "Invalid AI mode") from exc
        factory = runtime_factory
        if factory is None:
            from service.conversation.llm_runner import LlmRuntime
            factory = LlmRuntime
        runtime = factory(tenant_id=context[0], model_pack_id=pack.id, provider_max_retries=0)
        try:
            return await assist(payload, runtime, trace_id=request_id)
        except TimeoutError as exc:
            raise HTTPException(504, "规则整理超时，未保存规则；请保留当前输入。") from exc
        except ValueError as exc:
            raise HTTPException(502, "AI返回的规则格式或依据无效，未覆盖当前草稿。") from exc
        except Exception as exc:
            raise HTTPException(502, "规则整理服务暂时不可用，未保存规则。") from exc

    @router.post("/related")
    def related(payload: RelatedRequest, context: Annotated[tuple[str, str], Depends(trusted_context)]):
        return rank_related(payload)

    return router
