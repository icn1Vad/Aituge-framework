"""Rerank adapters for RAG retrieval candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from .models import RetrievalResult


class Reranker(Protocol):
    async def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        *,
        top_n: int,
        similarity_threshold: float = 0.0,
    ) -> list[RetrievalResult]:
        """Return candidates sorted by relevance."""


@dataclass(slots=True)
class ScoreReranker:
    """Default reranker: preserve retrieval scores and sort descending."""

    async def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        *,
        top_n: int,
        similarity_threshold: float = 0.0,
    ) -> list[RetrievalResult]:
        del query
        filtered = [
            candidate
            for candidate in candidates
            if candidate.score >= similarity_threshold
        ]
        return sorted(filtered, key=lambda item: item.score, reverse=True)[:top_n]


@dataclass(slots=True)
class OpenAICompatibleReranker:
    """Client for OpenAI-compatible `/v1/rerank` APIs.

    Expected response shape follows Jina/Cohere-style rerank APIs:
    `{"results": [{"index": 0, "relevance_score": 0.98}, ...]}`.
    """

    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: int = 30

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1/rerank"):
            return base
        if base.endswith("/v1"):
            return f"{base}/rerank"
        return f"{base}/v1/rerank"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = (
                self.api_key
                if self.api_key.lower().startswith("bearer ")
                else f"Bearer {self.api_key}"
            )
        return headers

    async def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        *,
        top_n: int,
        similarity_threshold: float = 0.0,
    ) -> list[RetrievalResult]:
        if not candidates:
            return []

        payload = {
            "model": self.model,
            "query": query,
            "documents": [candidate.content for candidate in candidates],
            "top_n": top_n,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.endpoint,
                headers=self._headers(),
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError("Rerank response missing list field 'results'.")

        reranked: list[RetrievalResult] = []
        for item in raw_results:
            index = item.get("index")
            if not isinstance(index, int) or index < 0 or index >= len(candidates):
                continue
            score = float(item.get("relevance_score", item.get("score", 0.0)))
            if score < similarity_threshold:
                continue
            original = candidates[index]
            reranked.append(
                RetrievalResult(
                    content=original.content,
                    score=score,
                    kb_id=original.kb_id,
                    chunk_id=original.chunk_id,
                    file_id=original.file_id,
                    title=original.title,
                    file_name=original.file_name,
                    url=original.url,
                    metadata=original.metadata,
                )
            )

        return sorted(reranked, key=lambda item: item.score, reverse=True)[:top_n]

