from __future__ import annotations

from dataclasses import dataclass, field

from contract.legal_evidence.models import (
    LegalEvidenceRelease,
    LegalRelation,
    LegalRetrievalUnit,
    LegalSearchCandidate,
)


@dataclass(slots=True)
class InMemoryLegalEvidenceRepository:
    """In-memory Adapter for tests at the planner Interface."""

    release: LegalEvidenceRelease | None = None
    exact_results: list[LegalSearchCandidate] = field(default_factory=list)
    keyword_pages: list[list[LegalSearchCandidate]] = field(default_factory=list)
    vector_pages: list[list[LegalSearchCandidate]] = field(default_factory=list)
    relations: dict[str, list[tuple[LegalRelation, LegalRetrievalUnit]]] = field(
        default_factory=dict
    )
    keyword_error: Exception | None = None
    exact_error: Exception | None = None
    vector_error: Exception | None = None

    def active_release(self) -> LegalEvidenceRelease | None:
        return self.release

    def exact_search(self, **_kwargs):
        if self.exact_error:
            raise self.exact_error
        return list(self.exact_results)

    @staticmethod
    def _page(
        pages: list[list[LegalSearchCandidate]], offset: int, limit: int
    ) -> list[LegalSearchCandidate]:
        page_index = offset // max(limit, 1)
        return list(pages[page_index]) if page_index < len(pages) else []

    def keyword_search(self, *, offset: int, limit: int, **_kwargs):
        if self.keyword_error:
            raise self.keyword_error
        return self._page(self.keyword_pages, offset, limit)

    def vector_search(self, *, offset: int, limit: int, **_kwargs):
        if self.vector_error:
            raise self.vector_error
        return self._page(self.vector_pages, offset, limit)

    def relation_neighbors(self, *, unit_id: str, **_kwargs):
        return list(self.relations.get(unit_id, ()))


@dataclass(frozen=True, slots=True)
class StaticLegalEmbeddingProvider:
    vector: list[float]
    profile_id: str = "test-profile"

    def embed_query(self, _text: str) -> list[float]:
        return list(self.vector)


@dataclass(frozen=True, slots=True)
class StaticLegalReranker:
    scores: dict[str, float]
    error: Exception | None = None

    def rerank(self, _query: str, candidates: list[LegalRetrievalUnit]):
        if self.error:
            raise self.error
        return {item.unit_id: self.scores[item.unit_id] for item in candidates}
