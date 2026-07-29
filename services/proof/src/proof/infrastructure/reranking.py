from __future__ import annotations

import time
from typing import Any

import httpx

from proof.application.retrieval import RerankScore
from proof.config import Settings
from proof.errors import ProofError
from proof.model_runtime import RerankerRuntimeConfig, build_proof_model_runtime


class OpenAICompatiblePolicyReranker:
    """Client for API or local OpenAI-compatible rerank endpoints."""

    def __init__(
        self,
        config: RerankerRuntimeConfig | Settings,
        *,
        instruction: str | None = None,
    ) -> None:
        default_instruction = ""
        if isinstance(config, Settings):
            default_instruction = config.rerank_instruction
            config = build_proof_model_runtime(config).reranker
        if config is None:
            raise ProofError(
                "reranker_unconfigured",
                "Reranker API is not configured.",
                status_code=503,
            )
        self.registration_id = config.id
        self.mode = config.mode
        self.endpoint = config.base_url.rstrip("/")
        if not self.endpoint.endswith("/reranks"):
            self.endpoint += "/reranks"
        self.api_key = config.api_key
        self.model = config.model.strip()
        self.timeout = config.timeout_seconds
        self.instruction = (instruction or default_instruction).strip()

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        *,
        top_n: int,
    ) -> list[RerankScore]:
        documents = [_rerank_document(candidate) for candidate in candidates]
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "instruct": self.instruction,
        }
        response = self._post(payload)
        try:
            raw_results = response.json().get("results") or []
            results = [
                RerankScore(
                    index=int(item["index"]),
                    score=float(item["relevance_score"]),
                )
                for item in raw_results
            ]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ProofError(
                "reranker_invalid_response",
                "Reranker returned an invalid response.",
                status_code=502,
            ) from exc
        if not results:
            raise ProofError(
                "reranker_invalid_response",
                "Reranker returned no results.",
                status_code=502,
            )
        return results

    def _post(self, payload: dict[str, Any]):
        last_reason = ""
        for attempt in range(3):
            try:
                response = httpx.post(
                    self.endpoint,
                    headers=(
                        {"Authorization": f"Bearer {self.api_key}"}
                        if self.api_key
                        else {}
                    ),
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                last_reason = type(exc).__name__
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise ProofError(
                    "reranker_unavailable",
                    "Reranker API request failed.",
                    status_code=503,
                    details={"reason": last_reason},
                ) from exc
            if response.status_code == 429 or response.status_code >= 500:
                last_reason = f"HTTP {response.status_code}"
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise ProofError(
                    "reranker_unavailable",
                    "Reranker API is temporarily unavailable.",
                    status_code=503,
                    details={"reason": last_reason},
                )
            if response.status_code >= 400:
                raise ProofError(
                    "reranker_request_rejected",
                    "Reranker API rejected the request.",
                    status_code=502,
                    details={"status_code": response.status_code},
                )
            return response
        raise AssertionError("unreachable")


def _rerank_document(candidate: dict[str, Any]) -> str:
    heading = " / ".join(str(value) for value in candidate.get("heading_path") or [])
    return "\n".join(
        value
        for value in (
            str(candidate.get("policy_title") or ""),
            str(candidate.get("clause_no_raw") or ""),
            heading,
            str(candidate.get("text") or ""),
        )
        if value
    )


# Backward-compatible import for existing integrations and tests.
DashScopePolicyReranker = OpenAICompatiblePolicyReranker
