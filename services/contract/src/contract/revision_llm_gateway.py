from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field


class _RemoteFinalizeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _FrameworkRevisionRemoteFinalizer:
    base_url: str
    internal_token: str = field(repr=False)
    tenant_id: str
    finalize_token: str = field(repr=False)
    connect_timeout_seconds: float
    read_timeout_seconds: float

    async def succeed(self) -> None:
        await self._finalize("SUCCESS", None)

    async def validation_failed(self, validation_code: str) -> None:
        await self._finalize("VALIDATION_FAILED", validation_code)

    async def _finalize(
        self,
        decision: str,
        validation_code: str | None,
    ) -> None:
        timeout = _timeout(
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.base_url.rstrip("/")
                + "/v1/internal/contract-revision-drafts:finalize",
                headers={
                    "X-Internal-Token": self.internal_token,
                    "X-Tenant-Id": self.tenant_id,
                },
                json={
                    "finalize_token": self.finalize_token,
                    "decision": decision,
                    "validation_code": validation_code,
                },
            )
        if response.is_success:
            return
        code = "MODEL_OBSERVABILITY_FINALIZE_FAILED"
        try:
            detail = response.json().get("detail")
            if isinstance(detail, dict) and isinstance(detail.get("code"), str):
                code = detail["code"]
        except Exception:
            pass
        raise _RemoteFinalizeError(code)


class _FrameworkRevisionCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    content: str
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    time_to_first_token_ms: int | None
    model_duration_ms: int
    trace_id: str
    provider_request_id: str | None
    finish_reason: str | None
    logical_call_id: str | None
    invocation_id: str | None
    model_attempt_no: int | None
    finalize_token: str | None = Field(exclude=True, repr=False)
    terminal_finalizer: Any | None = Field(default=None, exclude=True, repr=False)


@dataclass(frozen=True, slots=True)
class FrameworkRevisionLlmRuntime:
    base_url: str
    internal_token: str = field(repr=False)
    tenant_id: str
    connect_timeout_seconds: float
    read_timeout_seconds: float

    async def complete_with_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> _FrameworkRevisionCompletion:
        if (
            len(messages) != 1
            or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)
        ):
            raise ValueError("Revision completion accepts one user prompt")
        if not self.internal_token:
            raise RuntimeError("CONTRACT_INTERNAL_TOKEN is not configured")
        if kwargs.get("defer_terminal") is not True:
            raise ValueError("Revision completion requires deferred finalization")

        request_id = f"revision-{uuid.uuid4().hex}"
        timeout = _timeout(
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.base_url.rstrip("/")
                + "/v1/internal/contract-revision-drafts:complete",
                headers={
                    "X-Internal-Token": self.internal_token,
                    "X-Tenant-Id": self.tenant_id,
                    "X-Request-Id": request_id,
                },
                json={
                    "task": "GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY",
                    "model_id": model_id,
                    "user_prompt": messages[0]["content"],
                    "defer_terminal": True,
                    "logical_call_id": kwargs.get("logical_call_id"),
                    "model_attempt_no": kwargs.get("model_attempt_no"),
                    "fallback_from_invocation_id": kwargs.get(
                        "fallback_from_invocation_id"
                    ),
                },
            )
        response.raise_for_status()
        completion = _FrameworkRevisionCompletion.model_validate(response.json())
        if completion.finalize_token is None:
            return completion
        finalizer = _FrameworkRevisionRemoteFinalizer(
            base_url=self.base_url,
            internal_token=self.internal_token,
            tenant_id=self.tenant_id,
            finalize_token=completion.finalize_token,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
        )
        return completion.model_copy(update={"terminal_finalizer": finalizer})


def _timeout(connect_seconds: float, read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=connect_seconds,
        read=read_seconds,
        write=read_seconds,
        pool=connect_seconds,
    )
