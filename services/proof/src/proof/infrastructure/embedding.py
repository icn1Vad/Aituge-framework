from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

import httpx

from proof.config import Settings
from proof.errors import ProofError


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    id: str
    provider: str
    model: str
    dimensions: int


class OpenAICompatibleEmbeddingClient:
    batch_size = 10

    def __init__(self, settings: Settings) -> None:
        if not settings.embedding_configured:
            raise ProofError(
                "embedding_unconfigured",
                "Embedding API is not configured.",
                status_code=503,
            )
        self.base_url = settings.embedding_base_url.rstrip("/")
        self.api_key = settings.embedding_api_key
        self.model = settings.embedding_model
        self.dimensions = int(settings.embedding_dimensions or 0)
        self.timeout = settings.embedding_timeout_seconds
        profile_source = f"openai_compatible|{self.base_url}|{self.model}|{self.dimensions}"
        self.profile = EmbeddingProfile(
            id=hashlib.sha256(profile_source.encode("utf-8")).hexdigest()[:24],
            provider="openai_compatible",
            model=self.model,
            dimensions=self.dimensions,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            results.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return results

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        endpoint = self.base_url if self.base_url.endswith("/embeddings") else f"{self.base_url}/embeddings"
        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        last_error = ""
        for attempt in range(3):
            try:
                response = httpx.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise ProofError(
                    "embedding_unavailable",
                    "Embedding API request failed.",
                    status_code=503,
                    details={"reason": last_error},
                ) from exc

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise ProofError(
                    "embedding_unavailable",
                    "Embedding API is temporarily unavailable.",
                    status_code=503,
                    details={"reason": last_error},
                )
            if response.status_code >= 400:
                try:
                    error_body = response.json()
                except (TypeError, ValueError):
                    error_body = {}
                error_text = json.dumps(error_body, ensure_ascii=False).lower()
                too_long_markers = (
                    "too long",
                    "maximum context length",
                    "max input",
                    "input length",
                    "token limit",
                    "context_length_exceeded",
                    "输入过长",
                    "超过最大",
                )
                if response.status_code == 413 or any(marker in error_text for marker in too_long_markers):
                    raise ProofError(
                        "embedding_too_long",
                        "Embedding API cannot accept this complete clause.",
                        status_code=422,
                        details={"status_code": response.status_code},
                    )
                raise ProofError(
                    "embedding_request_rejected",
                    "Embedding API rejected the request.",
                    status_code=422,
                    details={"status_code": response.status_code},
                )
            try:
                data = response.json().get("data") or []
                ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
                vectors = [list(map(float, item["embedding"])) for item in ordered]
            except (KeyError, TypeError, ValueError) as exc:
                raise ProofError(
                    "embedding_invalid_response",
                    "Embedding API returned an invalid response.",
                    status_code=502,
                ) from exc
            if len(vectors) != len(texts):
                raise ProofError(
                    "embedding_invalid_response",
                    "Embedding API returned the wrong number of vectors.",
                    status_code=502,
                )
            if any(len(vector) != self.dimensions for vector in vectors):
                raise ProofError(
                    "embedding_dimension_mismatch",
                    "Embedding API returned vectors with an unexpected dimension.",
                    status_code=502,
                    details={"expected_dimensions": self.dimensions},
                )
            return vectors
        raise AssertionError("unreachable")
