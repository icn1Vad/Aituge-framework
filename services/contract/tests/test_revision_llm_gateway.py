from __future__ import annotations

import httpx
import asyncio
import pytest

from contract.revision_llm_gateway import FrameworkRevisionLlmRuntime


class _Client:
    requests: list[httpx.Request] = []

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
    ) -> httpx.Response:
        request = httpx.Request("POST", url, headers=headers, json=json)
        self.requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "content": '{"drafts":[]}',
                "prompt_tokens": 100,
                "cached_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 105,
                "model_duration_ms": 50,
                "trace_id": headers["X-Request-Id"],
                "provider_request_id": "provider-1",
                "finish_reason": "stop",
            },
        )


def test_revision_gateway_uses_hidden_framework_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Client.requests.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    runtime = FrameworkRevisionLlmRuntime(
        base_url="http://framework:8894/",
        internal_token="secret",
        tenant_id="tenant-1",
        connect_timeout_seconds=2,
        read_timeout_seconds=30,
    )

    result = asyncio.run(
        runtime.complete_with_usage(
            messages=[{"role": "user", "content": '{"draft_requests":[]}'}],
            model_id="model-1",
        )
    )

    assert result.prompt_tokens == 100
    request = _Client.requests[0]
    assert request.url.path == "/v1/internal/contract-revision-drafts:complete"
    assert request.headers["X-Internal-Token"] == "secret"
    assert request.headers["X-Tenant-Id"] == "tenant-1"
    assert request.read().decode() == (
        '{"task":"GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY",'
        '"model_id":"model-1","user_prompt":"{\\"draft_requests\\":[]}"}'
    )
