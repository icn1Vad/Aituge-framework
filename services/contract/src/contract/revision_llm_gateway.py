from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

import httpx
from pydantic import BaseModel, ConfigDict


class _FrameworkRevisionCompletion(BaseModel):
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


@dataclass(frozen=True, slots=True)
class FrameworkRevisionLlmRuntime:
    base_url: str
    internal_token: str
    tenant_id: str
    connect_timeout_seconds: float
    read_timeout_seconds: float

    async def complete_with_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        model_id: str,
        **_: Any,
    ) -> _FrameworkRevisionCompletion:
        if (
            len(messages) != 1
            or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)
        ):
            raise ValueError("Revision completion accepts one user prompt")
        if not self.internal_token:
            raise RuntimeError("CONTRACT_INTERNAL_TOKEN is not configured")

        request_id = f"revision-{uuid.uuid4().hex}"
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.read_timeout_seconds,
            pool=self.connect_timeout_seconds,
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
                },
            )
        response.raise_for_status()
        return _FrameworkRevisionCompletion.model_validate(response.json())
