from __future__ import annotations

import httpx

from aituge_model.config import ResolvedRerankerModel
from proof.config import Settings
from proof.infrastructure.reranking import DashScopePolicyReranker


class FakeResponse:
    status_code = 200

    def json(self):
        return {"results": [{"index": 0, "relevance_score": 0.91}]}


def test_reranker_sends_complete_clause_and_uses_embedding_key_fallback(monkeypatch) -> None:
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        _env_file=None,
        embedding_api_key="embedding-key",
        rerank_base_url="https://rerank.example/v1/reranks",
        rerank_model="qwen3-rerank",
    )
    long_text = "完整条款" * 3000
    client = DashScopePolicyReranker(settings)
    results = client.rerank(
        "审批流程",
        [{"policy_title": "制度", "clause_no_raw": "第一条", "heading_path": [], "text": long_text}],
        top_n=1,
    )

    assert results[0].score == 0.91
    assert long_text in captured["json"]["documents"][0]
    assert captured["headers"]["Authorization"] == "Bearer embedding-key"


def test_local_reranker_preserves_singular_vllm_endpoint(monkeypatch) -> None:
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    config = ResolvedRerankerModel(
        id="local-qwen3-rerank",
        mode="local",
        provider="openai_compatible",
        model="qwen3-reranker-8b",
        base_url="http://qwen-reranker-service:18082/v1/rerank",
        api_key="",
        timeout_seconds=30,
        instruction="Retrieve relevant text.",
    )
    client = DashScopePolicyReranker(config)

    results = client.rerank(
        "payment terms",
        [
            {
                "policy_title": "Contract",
                "clause_no_raw": "Clause 1",
                "heading_path": [],
                "text": "Payment deadline",
            }
        ],
        top_n=1,
    )

    assert results[0].score == 0.91
    assert captured["url"] == "http://qwen-reranker-service:18082/v1/rerank"
    assert captured["headers"] == {}
