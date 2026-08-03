from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from contract.revision_llm_gateway import FrameworkRevisionLlmRuntime


class _Client:
    requests: list[httpx.Request] = []
    finalize_status = 200

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
        json: dict,
    ) -> httpx.Response:
        request = httpx.Request("POST", url, headers=headers, json=json)
        self.requests.append(request)
        if request.url.path.endswith(":finalize"):
            if self.finalize_status == 200:
                return httpx.Response(
                    200,
                    request=request,
                    json={"status": "FINALIZED"},
                )
            return httpx.Response(
                self.finalize_status,
                request=request,
                json={
                    "detail": {
                        "code": "MODEL_INVOCATION_IDENTITY_CONFLICT"
                    }
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "content": '{"drafts":[]}',
                "prompt_tokens": 100,
                "cached_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 105,
                "time_to_first_token_ms": 9,
                "model_duration_ms": 50,
                "trace_id": headers["X-Request-Id"],
                "provider_request_id": "provider-1",
                "finish_reason": "stop",
                "logical_call_id": "logical-1",
                "invocation_id": "invocation-2",
                "model_attempt_no": 2,
                "finalize_token": "crf_revfin_v1." + "t" * 40,
            },
        )


def _runtime() -> FrameworkRevisionLlmRuntime:
    return FrameworkRevisionLlmRuntime(
        base_url="http://framework:8894/",
        internal_token="secret",
        tenant_id="tenant-1",
        connect_timeout_seconds=2,
        read_timeout_seconds=30,
    )


def test_revision_gateway_propagates_deferred_repair_identity_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Client.requests.clear()
    _Client.finalize_status = 200
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    runtime = _runtime()

    result = asyncio.run(
        runtime.complete_with_usage(
            messages=[{"role": "user", "content": '{"draft_requests":[]}'}],
            model_id="model-1",
            defer_terminal=True,
            logical_call_id="logical-1",
            model_attempt_no=2,
            fallback_from_invocation_id="invocation-1",
        )
    )

    assert result.prompt_tokens == 100
    assert result.time_to_first_token_ms == 9
    assert result.logical_call_id == "logical-1"
    assert result.invocation_id == "invocation-2"
    assert result.model_attempt_no == 2
    capability = "crf_revfin_v1." + "t" * 40
    assert capability not in repr(result)
    assert capability not in str(result)
    assert capability not in repr(result.terminal_finalizer)
    assert "secret" not in repr(runtime)
    request = _Client.requests[0]
    assert request.url.path == "/v1/internal/contract-revision-drafts:complete"
    assert request.headers["X-Internal-Token"] == "secret"
    assert request.headers["X-Tenant-Id"] == "tenant-1"
    assert json.loads(request.read()) == {
        "task": "GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY",
        "model_id": "model-1",
        "user_prompt": '{"draft_requests":[]}',
        "defer_terminal": True,
        "logical_call_id": "logical-1",
        "model_attempt_no": 2,
        "fallback_from_invocation_id": "invocation-1",
    }

    asyncio.run(result.terminal_finalizer.succeed())
    finalize = _Client.requests[1]
    assert finalize.url.path == "/v1/internal/contract-revision-drafts:finalize"
    assert finalize.read().decode() == (
        '{"finalize_token":"crf_revfin_v1.' + "t" * 40
        + '","decision":"SUCCESS","validation_code":null}'
    )


def test_revision_gateway_surfaces_remote_identity_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Client.requests.clear()
    _Client.finalize_status = 409
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = asyncio.run(
        _runtime().complete_with_usage(
            messages=[{"role": "user", "content": "{}"}],
            model_id="model-1",
            defer_terminal=True,
        )
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(
            result.terminal_finalizer.validation_failed(
                "REVISION_OUTPUT_SCHEMA_INVALID"
            )
        )
    assert getattr(raised.value, "code") == (
        "MODEL_INVOCATION_IDENTITY_CONFLICT"
    )
