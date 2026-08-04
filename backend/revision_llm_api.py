from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_observability.domain import (
    InvocationMetrics,
    MODEL_PROVIDER_FINISH_REASONS,
)
from model_observability.runtime import (
    RuntimeInvocationFinalizer,
    RuntimeInvocationHandle,
)
from service.conversation.llm_runner import LlmRuntime


REVISION_SYSTEM_PROMPT = (
    "你是合同条款修订器。风险已经由上游正式确认；你只生成可直接替换原条款的中文文案。"
    "严格返回JSON对象，不使用工具，不输出解释性前后缀。"
)
_FINALIZE_TOKEN_TTL_SECONDS = 300
_FINALIZE_TOKEN_CONTEXT = b"contract-revision-finalize-v1\0"
_FINALIZE_TOKEN_PREFIX = "crf_revfin_v1."


class RevisionCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(pattern="^GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY$")
    model_id: str = Field(min_length=1, max_length=160)
    user_prompt: str = Field(min_length=1, max_length=100_000)
    defer_terminal: Literal[True]
    logical_call_id: str | None = Field(default=None, min_length=1, max_length=80)
    # Older contract services do not send the observability retry fields. An
    # omitted attempt is therefore the first provider attempt; explicit null
    # is kept accepted for wire compatibility and normalized at the boundary.
    model_attempt_no: int | None = Field(default=1, ge=1)
    fallback_from_invocation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
    )


class RevisionCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    time_to_first_token_ms: int | None
    model_duration_ms: int
    trace_id: str
    provider_request_id: str | None = Field(default=None, max_length=512)
    finish_reason: str | None = Field(default=None, max_length=160)
    logical_call_id: str | None
    invocation_id: str | None
    model_attempt_no: int | None
    finalize_token: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "RevisionCompletionResponse":
        for name in ("provider_request_id", "finish_reason"):
            value = getattr(self, name)
            if value is not None and _contains_control_character(value):
                raise ValueError(f"{name} contains a control character")
        if self.finish_reason is not None and (
            self.finish_reason not in MODEL_PROVIDER_FINISH_REASONS
        ):
            raise ValueError("finish_reason is not registered")
        return self


class RevisionFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finalize_token: str = Field(min_length=40, max_length=4_096)
    decision: Literal["SUCCESS", "VALIDATION_FAILED"]
    validation_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{2,119}$",
    )

    @model_validator(mode="after")
    def validate_decision(self) -> "RevisionFinalizeRequest":
        if self.decision == "VALIDATION_FAILED" and not self.validation_code:
            raise ValueError("validation_code is required")
        if self.decision == "SUCCESS" and self.validation_code is not None:
            raise ValueError("validation_code is only valid for rejection")
        return self


class _FinalizeClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    tenant_id: str = Field(min_length=1, max_length=64)
    invocation_id: str = Field(min_length=1, max_length=80)
    logical_call_id: str = Field(min_length=1, max_length=80)
    model_attempt_no: int = Field(ge=1)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    model_duration_ms: int = Field(ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=512)
    finish_reason: str | None = Field(default=None, max_length=160)
    expires_at: int

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "_FinalizeClaims":
        for name in ("provider_request_id", "finish_reason"):
            value = getattr(self, name)
            if value is not None and _contains_control_character(value):
                raise ValueError(f"{name} contains a control character")
        if self.finish_reason is not None and (
            self.finish_reason not in MODEL_PROVIDER_FINISH_REASONS
        ):
            raise ValueError("finish_reason is not registered")
        return self


class RevisionFinalizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["FINALIZED"] = "FINALIZED"


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
        tenant = _authenticate(internal_token, tenant_id)
        model_attempt_no = payload.model_attempt_no or 1
        completion = await LlmRuntime(tenant).complete_with_usage(
            messages=[{"role": "user", "content": payload.user_prompt}],
            model_id=payload.model_id,
            system_prompt=REVISION_SYSTEM_PROMPT,
            max_tokens=4_000,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="revision_draft_mvp",
            trace_id=request_id,
            defer_terminal=True,
            logical_call_id=payload.logical_call_id,
            model_attempt_no=model_attempt_no,
            fallback_from_invocation_id=payload.fallback_from_invocation_id,
        )
        finalize_token = None
        if completion.terminal_finalizer is not None:
            if (
                not completion.logical_call_id
                or not completion.invocation_id
                or completion.model_attempt_no is None
            ):
                raise HTTPException(
                    status_code=503,
                    detail={"code": "MODEL_OBSERVABILITY_IDENTITY_UNAVAILABLE"},
                )
            claims = _FinalizeClaims(
                version=1,
                tenant_id=tenant,
                invocation_id=completion.invocation_id,
                logical_call_id=completion.logical_call_id,
                model_attempt_no=completion.model_attempt_no,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                model_duration_ms=completion.model_duration_ms,
                time_to_first_token_ms=completion.time_to_first_token_ms,
                provider_request_id=completion.provider_request_id,
                finish_reason=completion.finish_reason,
                expires_at=int(time.time()) + _FINALIZE_TOKEN_TTL_SECONDS,
            )
            finalize_token = _encode_finalize_token(claims, internal_token)

        values: dict[str, Any] = {
            name: (
                finalize_token
                if name == "finalize_token"
                else getattr(completion, name)
            )
            for name in RevisionCompletionResponse.model_fields
        }
        return RevisionCompletionResponse.model_validate(values)

    @router.post(
        "/v1/internal/contract-revision-drafts:finalize",
        response_model=RevisionFinalizeResponse,
        include_in_schema=False,
    )
    async def finalize_revision_drafts(
        payload: RevisionFinalizeRequest,
        internal_token: Annotated[str, Header(alias="X-Internal-Token")],
        tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    ) -> RevisionFinalizeResponse:
        tenant = _authenticate(internal_token, tenant_id)
        try:
            claims = _decode_finalize_token(payload.finalize_token, internal_token)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"code": "REVISION_FINALIZE_TOKEN_INVALID"},
            ) from None
        if claims.tenant_id != tenant or claims.expires_at <= int(time.time()):
            raise HTTPException(
                status_code=400,
                detail={"code": "REVISION_FINALIZE_TOKEN_INVALID"},
            )

        runtime = LlmRuntime(tenant)
        recorder = runtime.invocation_recorder
        if recorder is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "MODEL_OBSERVABILITY_FINALIZE_FAILED"},
            )
        finalizer = RuntimeInvocationFinalizer(
            recorder=recorder,
            handle=RuntimeInvocationHandle(
                invocation_id=claims.invocation_id,
                logical_call_id=claims.logical_call_id,
                attempt_no=claims.model_attempt_no,
            ),
            metrics=InvocationMetrics(
                input_tokens=claims.prompt_tokens,
                output_tokens=claims.completion_tokens,
                latency_ms=claims.model_duration_ms,
                time_to_first_token_ms=claims.time_to_first_token_ms,
            ),
            provider_request_id=claims.provider_request_id,
            finish_reason=claims.finish_reason,
        )
        try:
            if payload.decision == "SUCCESS":
                await finalizer.succeed()
            else:
                await finalizer.validation_failed(payload.validation_code or "")
        except Exception as exc:
            if _is_identity_conflict(exc):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "MODEL_INVOCATION_IDENTITY_CONFLICT"},
                ) from None
            raise HTTPException(
                status_code=503,
                detail={"code": "MODEL_OBSERVABILITY_FINALIZE_FAILED"},
            ) from None
        return RevisionFinalizeResponse()

    return router


def _authenticate(internal_token: str, tenant_id: str | None) -> str:
    expected = os.getenv("CONTRACT_INTERNAL_TOKEN", "")
    if not expected or not hmac.compare_digest(internal_token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal credential")
    tenant = str(tenant_id or "").strip()
    if not tenant or tenant.lower() in {"null", "none", "undefined"}:
        raise HTTPException(status_code=400, detail="X-Tenant-Id is required.")
    return tenant


def _encode_finalize_token(claims: _FinalizeClaims, secret: str) -> str:
    raw = json.dumps(
        claims.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        _FINALIZE_TOKEN_CONTEXT + raw,
        hashlib.sha256,
    ).digest()
    return _FINALIZE_TOKEN_PREFIX + _b64(raw) + "." + _b64(signature)


def _decode_finalize_token(token: str, secret: str) -> _FinalizeClaims:
    try:
        if not token.startswith(_FINALIZE_TOKEN_PREFIX):
            raise ValueError("invalid token")
        encoded_payload, encoded_signature = token[
            len(_FINALIZE_TOKEN_PREFIX):
        ].split(".", 1)
        raw = _unb64(encoded_payload)
        supplied = _unb64(encoded_signature)
    except Exception:
        raise ValueError("invalid token") from None
    expected = hmac.new(
        secret.encode("utf-8"),
        _FINALIZE_TOKEN_CONTEXT + raw,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("invalid token")
    try:
        return _FinalizeClaims.model_validate_json(raw)
    except Exception:
        raise ValueError("invalid token") from None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _is_identity_conflict(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in {
        "MODEL_INVOCATION_DUPLICATE_ATTEMPT",
        "MODEL_INVOCATION_IDENTITY_CONFLICT",
        "MODEL_INVOCATION_INVALID_TRANSITION",
        "MODEL_INVOCATION_LOGICAL_CALL_CONFLICT",
    }:
        return True
    return str(exc).startswith("model invocation already finalized as ")
