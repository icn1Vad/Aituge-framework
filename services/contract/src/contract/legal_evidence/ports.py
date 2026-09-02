from __future__ import annotations

from typing import Protocol

from contract.legal_evidence.models import (
    LegalEvidenceRelease,
    LegalRelation,
    LegalRetrievalUnit,
    LegalSearchCandidate,
)


class LegalEvidenceRepository(Protocol):
    """Storage Interface used by the planner.

    Implementations page candidates for execution safety; page size is not a
    business result limit. The planner continues requesting pages until issue
    coverage is sufficient or a physical safety budget is reached.
    """

    def active_release(self) -> LegalEvidenceRelease | None: ...

    def exact_search(
        self,
        *,
        release_id: str,
        references: list[tuple[str, str | None]],
        jurisdiction: str | None,
        as_of_date: str,
        limit: int,
    ) -> list[LegalSearchCandidate]: ...

    def keyword_search(
        self,
        *,
        release_id: str,
        query: str,
        jurisdiction: str | None,
        as_of_date: str,
        offset: int,
        limit: int,
    ) -> list[LegalSearchCandidate]: ...

    def vector_search(
        self,
        *,
        release_id: str,
        query_vector: list[float],
        embedding_profile_id: str,
        jurisdiction: str | None,
        as_of_date: str,
        offset: int,
        limit: int,
    ) -> list[LegalSearchCandidate]: ...

    def relation_neighbors(
        self,
        *,
        release_id: str,
        unit_id: str,
        limit: int = 128,
    ) -> list[tuple[LegalRelation, LegalRetrievalUnit]]: ...


class LegalEmbeddingProvider(Protocol):
    profile_id: str

    def embed_query(self, text: str) -> list[float]: ...


class LegalReranker(Protocol):
    def rerank(
        self,
        query: str,
        candidates: list[LegalRetrievalUnit],
    ) -> dict[str, float]: ...
