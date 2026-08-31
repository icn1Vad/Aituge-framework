from __future__ import annotations

import math

import httpx

from contract.legal_evidence.models import LegalRetrievalUnit


class OpenAICompatibleLegalReranker:
    """Synchronous Adapter for model-gateway's Qwen3 rerank endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        registration_id: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30,
        maximum_query_chars: int = 4000,
        maximum_document_chars: int = 8000,
    ) -> None:
        base = base_url.rstrip("/")
        self.endpoint = (
            base
            if base.endswith(("/rerank", "/reranks"))
            else base + "/reranks"
        )
        self.registration_id = registration_id
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.maximum_query_chars = maximum_query_chars
        self.maximum_document_chars = maximum_document_chars

    def rerank(
        self,
        query: str,
        candidates: list[LegalRetrievalUnit],
    ) -> dict[str, float]:
        if not candidates:
            return {}
        documents = [self._document(item) for item in candidates]
        headers = {
            "X-Aituge-Model-Component-ID": self.registration_id,
            "X-Request-ID": "legal-evidence-rerank",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = httpx.post(
            self.endpoint,
            headers=headers,
            json={
                "model": self.model,
                "query": query[: self.maximum_query_chars],
                "documents": documents,
                # Return every candidate. Coverage logic, not top_n, decides
                # how many legal sources enter the final bundle.
                "top_n": len(documents),
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise TypeError("Rerank response is missing results")
        scores: dict[str, float] = {}
        for result in results:
            if not isinstance(result, dict):
                raise TypeError("Rerank result is invalid")
            index = result.get("index")
            score = result.get("relevance_score", result.get("score"))
            if not isinstance(index, int) or not 0 <= index < len(candidates):
                raise ValueError("Rerank result index is invalid")
            value = float(score)
            if not math.isfinite(value):
                raise ValueError("Rerank score is invalid")
            scores[candidates[index].unit_id] = max(0.0, min(1.0, value))
        if set(scores) != {item.unit_id for item in candidates}:
            raise ValueError("Rerank response did not cover every candidate")
        return scores

    def _document(self, unit: LegalRetrievalUnit) -> str:
        value = "\n".join(
            item for item in (unit.title, unit.article_no or "", unit.content) if item
        )
        return value[: self.maximum_document_chars]
