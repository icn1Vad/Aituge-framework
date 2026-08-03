from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.revision_llm_api as revision_llm_api


@dataclass
class _Completion:
    content: str = '{"drafts":[]}'
    prompt_tokens: int = 100
    cached_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 105
    time_to_first_token_ms: int | None = 8
    model_duration_ms: int = 25
    trace_id: str = "request-1"
    provider_request_id: str = "provider-1"
    finish_reason: str = "stop"
    terminal_finalizer: object | None = None
    logical_call_id: str | None = None
    invocation_id: str | None = None
    model_attempt_no: int | None = None


class _Runtime:
    calls: list[dict] = []

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    async def complete_with_usage(self, **kwargs):
        self.calls.append({"tenant_id": self.tenant_id, **kwargs})
        return _Completion(trace_id=kwargs["trace_id"])


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("CONTRACT_INTERNAL_TOKEN", "secret")
    monkeypatch.setattr(revision_llm_api, "LlmRuntime", _Runtime)
    app = FastAPI()
    app.include_router(revision_llm_api.create_revision_llm_router())
    return TestClient(app)


def test_revision_completion_is_hidden_and_authenticated(monkeypatch) -> None:
    client = _client(monkeypatch)
    payload = {
        "task": "GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY",
        "model_id": "model-1",
        "user_prompt": '{"draft_requests":[]}',
        "defer_terminal": True,
    }

    assert client.post(
        "/v1/internal/contract-revision-drafts:complete",
        headers={"X-Internal-Token": "wrong", "X-Tenant-Id": "tenant-1"},
        json=payload,
    ).status_code == 401
    assert client.post(
        "/v1/internal/contract-revision-drafts:complete",
        headers={"X-Internal-Token": "secret"},
        json=payload,
    ).status_code == 400

    response = client.post(
        "/v1/internal/contract-revision-drafts:complete",
        headers={
            "X-Internal-Token": "secret",
            "X-Tenant-Id": "tenant-1",
            "X-Request-Id": "request-9",
        },
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["prompt_tokens"] == 100
    assert _Runtime.calls[-1]["tenant_id"] == "tenant-1"
    assert _Runtime.calls[-1]["thinking_override"] is False
    assert _Runtime.calls[-1]["temperature"] == 0
    assert (
        "/v1/internal/contract-revision-drafts:complete"
        not in client.get("/openapi.json").json()["paths"]
    )
