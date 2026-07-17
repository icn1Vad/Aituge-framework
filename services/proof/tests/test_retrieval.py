from __future__ import annotations

import pytest

from proof.application.retrieval import (
    HybridPolicyRetriever,
    RerankScore,
    RetrievalBranchResult,
    RetrievalFilters,
)
from proof.errors import ProofError


def candidate(unit_id: str, text_hash: str, score: float) -> dict:
    return {
        "id": unit_id,
        "text_hash": text_hash,
        "score": score,
        "text": f"text-{unit_id}",
        "policy_title": "测试制度",
        "clause_no_raw": "第一条",
        "clause_ordinal": 1,
    }


class FakeRetriever:
    def __init__(self, name: str, items=None, error: Exception | None = None) -> None:
        self.name = name
        self.items = items or []
        self.error = error

    def retrieve(self, query, *, limit, filters):
        if self.error:
            raise self.error
        return RetrievalBranchResult(self.items[:limit], {"name": self.name})


class FakeReranker:
    model = "test-reranker"

    def rerank(self, query, candidates, *, top_n):
        return [RerankScore(index=index, score=1.0 - index / 10) for index in range(top_n)]


def test_hybrid_retrieval_deduplicates_and_reranks() -> None:
    retriever = HybridPolicyRetriever(
        keyword=FakeRetriever("keyword", [candidate("u1", "same", 3), candidate("u2", "k2", 1)]),
        vector=FakeRetriever("vector", [candidate("u3", "same", 0.9), candidate("u4", "v2", 0.8)]),
        reranker=FakeReranker(),
    )
    result = retriever.search("审批", top_k=2, mode="hybrid", filters=RetrievalFilters())

    assert result["reranker_used"] is True
    assert result["degraded"] is False
    assert result["candidate_counts"] == {"keyword": 2, "vector": 2, "deduplicated": 3, "returned": 2}
    assert result["results"][0]["retrieval_sources"] == ["keyword", "vector"]


def test_hybrid_retrieval_degrades_to_one_recall_path() -> None:
    retriever = HybridPolicyRetriever(
        keyword=FakeRetriever("keyword", [candidate("u1", "h1", 2)]),
        vector=FakeRetriever("vector", error=ProofError("embedding_unavailable", "down", status_code=503)),
        reranker=None,
    )
    result = retriever.search("审批", top_k=2, mode="hybrid", filters=RetrievalFilters())

    assert result["results"][0]["id"] == "u1"
    assert result["degraded"] is True
    assert "vector:embedding_unavailable" in result["degradation_reasons"]


def test_retrieval_fails_only_when_every_selected_path_fails() -> None:
    retriever = HybridPolicyRetriever(
        keyword=FakeRetriever("keyword", error=RuntimeError("down")),
        vector=FakeRetriever("vector", error=RuntimeError("down")),
        reranker=None,
    )
    with pytest.raises(ProofError) as exc_info:
        retriever.search("审批", top_k=2, mode="hybrid", filters=RetrievalFilters())
    assert exc_info.value.code == "retrieval_unavailable"


def test_invalid_retrieval_mode_is_rejected() -> None:
    retriever = HybridPolicyRetriever(
        keyword=FakeRetriever("keyword"),
        vector=FakeRetriever("vector"),
        reranker=None,
    )
    with pytest.raises(ProofError) as exc_info:
        retriever.search("审批", top_k=2, mode="unknown", filters=RetrievalFilters())
    assert exc_info.value.code == "invalid_retrieval_mode"
