"""Read-only RAG retrieval service shaped after PAI-RAG's query side."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .backend import InMemoryRagBackend, RagRetrievalBackend
from .fusion_reranker import arerank_fusion
from .models import (
    KnowledgeBase,
    KnowledgeBaseChunk,
    KnowledgeBaseFile,
    RetrievalResult,
    RetrievalSettings,
)
from .rerank import Reranker


@dataclass(slots=True)
class RagRetrievalService:
    """Orchestrate kb retrieval, fusion, rerank, and bounded read tools.

    This intentionally mirrors PAI-RAG's online retrieval side: the service owns
    settings and routing, while concrete candidate retrieval lives behind a
    backend adapter.
    """

    backend: RagRetrievalBackend
    default_settings: RetrievalSettings = field(default_factory=RetrievalSettings)
    reranker: Reranker | None = None

    @classmethod
    def from_in_memory(
        cls,
        *,
        knowledgebases: list[KnowledgeBase],
        files: list[KnowledgeBaseFile] | None = None,
        chunks: list[KnowledgeBaseChunk] | None = None,
        default_settings: RetrievalSettings | None = None,
        reranker: Reranker | None = None,
    ) -> "RagRetrievalService":
        return cls(
            backend=InMemoryRagBackend(
                knowledgebases=knowledgebases,
                files=files or [],
                chunks=chunks or [],
            ),
            default_settings=default_settings or RetrievalSettings(),
            reranker=reranker,
        )

    def get_knowledgebase(self, kb_id: str) -> KnowledgeBase:
        return self.backend.get_knowledgebase(kb_id)

    async def catalog(
        self,
        *,
        kb_id: str,
        query: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self.backend.catalog(kb_id=kb_id, query=query, limit=limit)

    async def grep(
        self,
        *,
        kb_id: str,
        pattern: str,
        context: int = 2,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self.backend.grep(
            kb_id=kb_id,
            pattern=pattern,
            context=context,
            limit=limit,
        )

    async def fetch(
        self,
        *,
        kb_id: str,
        file_id: str | None = None,
        chunk_id: str | None = None,
        offset: int = 0,
        max_chars: int = 6000,
    ) -> dict[str, Any]:
        return await self.backend.fetch(
            kb_id=kb_id,
            file_id=file_id,
            chunk_id=chunk_id,
            offset=offset,
            max_chars=max_chars,
        )

    async def search(
        self,
        *,
        kb_id: str,
        query: str,
        settings: RetrievalSettings | None = None,
    ) -> list[RetrievalResult]:
        self.get_knowledgebase(kb_id)
        settings = settings or self.default_settings

        text_results: list[RetrievalResult] = []
        dense_results: list[RetrievalResult] = []
        if settings.retrieval_mode in ("keyword", "hybrid"):
            text_results = await self.backend.text_search(
                kb_id=kb_id,
                query=query,
                top_k=settings.top_k,
            )
        if settings.retrieval_mode in ("vector", "hybrid"):
            dense_results = await self.backend.vector_search(
                kb_id=kb_id,
                query=query,
                top_k=settings.top_k,
            )

        reranker = (
            self.reranker
            if settings.enable_rerank and len(text_results) + len(dense_results) > 1
            else None
        )
        return await arerank_fusion(
            query=query,
            text_results=text_results,
            dense_results=dense_results,
            rerank_model=reranker,
            vector_weight=settings.vector_weight,
            top_k=settings.top_k,
            rerank_top_k=settings.rerank_top_k or settings.top_k,
            similarity_threshold=settings.similarity_threshold,
        )

