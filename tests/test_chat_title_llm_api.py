from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import backend.chat_title_llm_api as chat_title_llm_api
from service.conversation.title_generator import (
    ConversationTitleGenerator,
    normalize_conversation_title,
)


class _ApiRuntime:
    calls: list[dict] = []

    def __init__(self, tenant_id: str, model_pack_id: str) -> None:
        self.calls.append(
            {"tenant_id": tenant_id, "model_pack_id": model_pack_id}
        )


class _ApiGenerator:
    calls: list[dict] = []

    def __init__(self, runtime: _ApiRuntime) -> None:
        self.runtime = runtime

    async def generate(self, question: str, *, trace_id: str | None = None) -> str:
        self.calls.append({"question": question, "trace_id": trace_id})
        return "采购审批流程"


def _client(monkeypatch) -> TestClient:
    _ApiRuntime.calls.clear()
    _ApiGenerator.calls.clear()
    monkeypatch.setenv("CONTRACT_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("FRAMEWORK_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(chat_title_llm_api, "LlmRuntime", _ApiRuntime)
    monkeypatch.setattr(
        chat_title_llm_api,
        "ConversationTitleGenerator",
        _ApiGenerator,
    )
    monkeypatch.setattr(
        chat_title_llm_api,
        "get_model_pack_for_ai_mode",
        lambda mode: SimpleNamespace(id=f"{mode}-pack"),
    )
    app = FastAPI()
    app.include_router(chat_title_llm_api.create_chat_title_llm_router())
    return TestClient(app)


@pytest.mark.parametrize("ai_mode", ["public", "private"])
def test_title_endpoint_resolves_model_pack_and_context(monkeypatch, ai_mode) -> None:
    client = _client(monkeypatch)
    response = client.post(
        "/v1/internal/chat-titles:generate",
        headers={
            "X-Internal-Token": "secret",
            "X-Tenant-Id": "tenant-1",
            "X-User-Id": "user-1",
            "X-Request-Id": "request-1",
            "X-AI-Mode": ai_mode,
        },
        json={"question": "采购审批应该经过哪些流程？"},
    )

    assert response.status_code == 200
    assert response.json() == {"title": "采购审批流程"}
    assert _ApiRuntime.calls == [
        {"tenant_id": "tenant-1", "model_pack_id": f"{ai_mode}-pack"}
    ]
    assert _ApiGenerator.calls == [
        {"question": "采购审批应该经过哪些流程？", "trace_id": "request-1"}
    ]
    assert (
        "/v1/internal/chat-titles:generate"
        not in client.get("/openapi.json").json()["paths"]
    )


def test_title_endpoint_requires_internal_context(monkeypatch) -> None:
    client = _client(monkeypatch)

    assert client.post(
        "/v1/internal/chat-titles:generate",
        headers={
            "X-Internal-Token": "wrong",
            "X-Tenant-Id": "tenant-1",
            "X-User-Id": "user-1",
        },
        json={"question": "问题"},
    ).status_code == 401
    assert client.post(
        "/v1/internal/chat-titles:generate",
        headers={"X-Internal-Token": "secret", "X-User-Id": "user-1"},
        json={"question": "问题"},
    ).status_code == 400
    assert client.post(
        "/v1/internal/chat-titles:generate",
        headers={"X-Internal-Token": "secret", "X-Tenant-Id": "tenant-1"},
        json={"question": "问题"},
    ).status_code == 400


@dataclass
class _Completion:
    content: str


class _GeneratorRuntime:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []
        self.model_runtime_provider = SimpleNamespace(
            active_pack=SimpleNamespace(
                llm=SimpleNamespace(id="pack-llm"),
            )
        )

    async def complete_with_usage(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self.content)


def test_title_generator_uses_active_pack_llm_without_tools() -> None:
    runtime = _GeneratorRuntime('{"title":"采购审批流程"}')

    title = asyncio.run(
        ConversationTitleGenerator(runtime).generate(
            "采购审批应该经过哪些流程？",
            trace_id="request-2",
        )
    )

    assert title == "采购审批流程"
    assert runtime.calls[0]["model_id"] == "pack-llm"
    assert runtime.calls[0]["thinking_override"] is False
    assert runtime.calls[0]["response_format"] == {"type": "json_object"}
    assert runtime.calls[0]["messages"] == [
        {"role": "user", "content": "采购审批应该经过哪些流程？"}
    ]


def test_title_normalization_rejects_invalid_json_and_truncates_unicode() -> None:
    assert normalize_conversation_title(
        '{"title":"一二三四五六七八九十十一"}'
    ) == "一二三四五六七八九十"
    with pytest.raises(ValueError):
        normalize_conversation_title("not-json")
    with pytest.raises(ValueError):
        normalize_conversation_title('{"title":"第一行\\n第二行"}')
