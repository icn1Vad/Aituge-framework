from __future__ import annotations

import pytest

from proof.application.conflict_retrieval import ConflictRetrievalService
from proof.application.conflict_retrieval.service import _select_reranked_evidence
from proof.application.retrieval import RerankScore
from proof.errors import ProofError
from proof.infrastructure.embedding import EmbeddingProfile


def _candidate(unit_id: str, text_hash: str, policy_id: str, score: float) -> dict:
    return {
        "id": unit_id,
        "document_id": f"d-{policy_id}",
        "policy_id": policy_id,
        "policy_title": "采购管理办法",
        "policy_version": "1.0",
        "original_name": "采购管理办法.docx",
        "clause_no_raw": "第一条",
        "clause_ordinal": 1,
        "heading_path": [],
        "text": f"候选条款 {unit_id}",
        "text_hash": text_hash,
        "score": score,
    }


class FakeEmbeddingClient:
    profile = EmbeddingProfile(id="test-profile", provider="test", model="test", dimensions=3)

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[1.0, 0.0, 0.0]]


class FakeConflictRepository:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.same_title_policy_ids = ["same-policy"]
        self.source = {
            "id": "source-unit",
            "document_id": "source-document",
            "policy_id": "source-policy",
            "policy_title": "采购管理办法（试行）",
            "normalized_title": "采购管理",
            "policy_version": "1.0",
            "policy_status": "draft",
            "category_code": "procurement_supply",
            "category_name": "采购、招投标与供应商",
            "clause_no_raw": "第一条",
            "clause_ordinal": 1,
            "heading_path": [],
            "text": "第一条 采购审批权限为一百万元。",
            "text_hash": "source-hash",
            "original_name": "source.docx",
        }

    def get_conflict_source_unit(self, unit_id: str):
        return self.source if unit_id == "source-unit" else None

    def resolve_unique_near_unit_id(self, unit_id: str):
        return "source-unit" if unit_id == "source-unix" else None

    def find_effective_policy_ids_by_normalized_title(self, normalized_title, *, exclude_policy_id):
        assert normalized_title == "采购管理"
        assert exclude_policy_id == "source-policy"
        return self.same_title_policy_ids

    def get_category_context(self, category_code: str):
        assert category_code == "procurement_supply"
        return {
            "code": category_code,
            "level": 2,
            "parent_code": "procurement_transactions",
            "parent_category_codes": ["contract_transaction", "procurement_supply"],
            "child_category_codes": [],
        }

    def vector_search(self, **values):
        self.calls.append(values)
        if values["policy_ids"]:
            assert values["excluded_policy_ids"] == ["source-policy"]
            return [
                _candidate("same-1", "shared-hash", "same-policy", 0.99),
                _candidate("same-2", "same-2", "same-policy", 0.90),
            ]
        assert values["excluded_policy_ids"] == ["source-policy", *self.same_title_policy_ids]
        if values["category_codes"] == ["procurement_supply"]:
            return [
                _candidate("leaf-duplicate", "shared-hash", "leaf-policy", 0.95),
                _candidate("leaf-2", "leaf-2", "leaf-policy", 0.85),
            ]
        if values["category_codes"]:
            return [_candidate("parent-1", "parent-1", "parent-policy", 0.80)]
        return [_candidate("global-1", "global-1", "global-policy", 0.75)]


class FakeReranker:
    model = "test-reranker"

    def __init__(self) -> None:
        self.top_ns: list[int] = []

    def rerank(self, query, candidates, *, top_n):
        self.top_ns.append(top_n)
        return [
            RerankScore(index=index, score=1.0 - index / 100)
            for index in range(min(top_n, len(candidates)))
        ]


class FailingReranker:
    model = "failing-reranker"

    def rerank(self, query, candidates, *, top_n):
        raise RuntimeError("down")


class IncompleteReranker:
    model = "incomplete-reranker"

    def rerank(self, query, candidates, *, top_n):
        return [RerankScore(index=0, score=1.0)]


def test_conflict_retrieval_runs_four_vector_branches_with_one_embedding() -> None:
    repository = FakeConflictRepository()
    embedding = FakeEmbeddingClient()
    reranker = FakeReranker()
    service = ConflictRetrievalService(
        repository=repository,
        embedding_client=embedding,
        reranker=reranker,
    )

    result = service.retrieve_for_unit("source-unit", top_k=5)

    assert len(embedding.calls) == 1
    assert len(repository.calls) == 4
    assert reranker.top_ns == [3]
    assert {call["top_k"] for call in repository.calls} == {2, 4, 6}
    assert result.reranker_used is True
    assert result.candidate_counts == {
        "same_title": 2,
        "leaf_category": 2,
        "parent_category": 1,
        "global": 1,
        "deduplicated": 5,
        "returned": 5,
    }
    assert [item["ref"] for item in result.results] == ["C01", "C02", "C03", "C04", "C05"]
    shared = result.results[0]
    assert shared["retrieval_sources"] == ["same_title", "leaf_category"]
    assert shared["branch_ranks"] == {"same_title": 1, "leaf_category": 1}
    assert shared["citation"]["policy_id"] == "same-policy"


def test_conflict_retrieval_falls_back_to_stable_branch_priority() -> None:
    service = ConflictRetrievalService(
        repository=FakeConflictRepository(),
        embedding_client=FakeEmbeddingClient(),
        reranker=FailingReranker(),
    )

    result = service.retrieve_for_unit("source-unit", top_k=2)

    assert result.reranker_used is False
    assert result.degraded is True
    assert "reranker:RuntimeError" in result.degradation_reasons
    assert [item["id"] for item in result.results] == ["same-1", "same-2"]
    assert [item["ref"] for item in result.results] == ["C01", "C02"]


def test_conflict_retrieval_gives_all_slots_to_reranker_without_same_title_policy() -> None:
    repository = FakeConflictRepository()
    repository.same_title_policy_ids = []
    reranker = FakeReranker()
    service = ConflictRetrievalService(
        repository=repository,
        embedding_client=FakeEmbeddingClient(),
        reranker=reranker,
    )

    result = service.retrieve_for_unit("source-unit", top_k=10)

    assert reranker.top_ns == [4]
    assert all("same_title" not in item["retrieval_sources"] for item in result.results)
    assert result.skipped_branches == ["same_title:no_matching_policy"]


def test_conflict_retrieval_falls_back_when_reranker_returns_partial_results() -> None:
    service = ConflictRetrievalService(
        repository=FakeConflictRepository(),
        embedding_client=FakeEmbeddingClient(),
        reranker=IncompleteReranker(),
    )

    result = service.retrieve_for_unit("source-unit", top_k=2)

    assert result.reranker_used is False
    assert result.degraded is True
    assert "reranker:reranker_invalid_response" in result.degradation_reasons
    assert [item["id"] for item in result.results] == ["same-1", "same-2"]
    assert [item["ref"] for item in result.results] == ["C01", "C02"]


def test_conflict_retrieval_rejects_unknown_source_unit() -> None:
    service = ConflictRetrievalService(
        repository=FakeConflictRepository(),
        embedding_client=FakeEmbeddingClient(),
    )

    with pytest.raises(ProofError) as exc_info:
        service.retrieve_for_unit("missing")

    assert exc_info.value.code == "retrieval_unit_not_found"


def test_conflict_retrieval_safely_corrects_one_character_unit_id_copy_error() -> None:
    service = ConflictRetrievalService(
        repository=FakeConflictRepository(),
        embedding_client=FakeEmbeddingClient(),
    )

    result = service.retrieve_for_unit("source-unix", top_k=1)

    assert result.source["id"] == "source-unit"
    assert result.source["requested_unit_id"] == "source-unix"
    assert result.source["unit_id_corrected"] is True


def test_conflict_rerank_keeps_six_normalized_title_units_and_adds_non_title_results() -> None:
    same_title = [
        {
            "id": f"same-{index}",
            "text_hash": f"same-{index}",
            "retrieval_sources": ["same_title"],
            "branch_ranks": {"same_title": index + 1},
        }
        for index in range(6)
    ]
    other = [
        {
            "id": f"other-{index}",
            "text_hash": f"other-{index}",
            "retrieval_sources": ["leaf_category"],
            "branch_ranks": {"leaf_category": index + 1},
        }
        for index in range(6)
    ]
    candidates = [*same_title, *other]
    reranked = [
        {**candidate, "rerank_rank": rank, "rerank_score": 1 - rank / 100}
        for rank, candidate in enumerate(reversed(other), start=1)
    ]

    result = _select_reranked_evidence(candidates, reranked, limit=10)

    assert [item["id"] for item in result] == [
        *(f"same-{index}" for index in range(6)),
        "other-5",
        "other-4",
        "other-3",
        "other-2",
    ]
    assert "rerank_rank" not in result[0]
    assert result[6]["rerank_rank"] == 1


def test_conflict_rerank_deduplicates_branch_representatives() -> None:
    candidates = [
        {
            "id": "shared",
            "text_hash": "shared",
            "retrieval_sources": ["same_title", "leaf_category"],
            "branch_ranks": {"same_title": 1, "leaf_category": 1},
        },
        {"id": "parent", "text_hash": "parent", "retrieval_sources": ["parent_category"], "branch_ranks": {"parent_category": 1}},
        {"id": "global", "text_hash": "global", "retrieval_sources": ["global"], "branch_ranks": {"global": 1}},
        {"id": "other", "text_hash": "other", "retrieval_sources": ["global"], "branch_ranks": {"global": 2}},
    ]
    reranked = [
        {**candidates[3], "rerank_rank": 1, "rerank_score": 0.99},
        {**candidates[1], "rerank_rank": 2, "rerank_score": 0.98},
        {**candidates[2], "rerank_rank": 3, "rerank_score": 0.97},
        {**candidates[0], "rerank_rank": 4, "rerank_score": 0.96},
    ]

    result = _select_reranked_evidence(candidates, reranked, limit=4)

    assert [item["id"] for item in result] == ["shared", "other", "parent", "global"]
