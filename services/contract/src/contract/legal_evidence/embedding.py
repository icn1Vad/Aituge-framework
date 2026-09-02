from __future__ import annotations

import hashlib
import math
import time
import uuid

import httpx


class OpenAICompatibleLegalEmbeddingProvider:
    """Small Adapter for the shared model-gateway embedding Interface."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        registration_id: str,
        model: str,
        dimensions: int = 1024,
        timeout_seconds: float = 30,
        maximum_input_chars: int = 8000,
        maximum_attempts: int = 4,
        retry_base_seconds: float = 0.5,
    ) -> None:
        if maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds cannot be negative")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.registration_id = registration_id
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.maximum_input_chars = maximum_input_chars
        self.maximum_attempts = maximum_attempts
        self.retry_base_seconds = retry_base_seconds
        identity = f"{registration_id}|{model}|{dimensions}"
        self.profile_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.provider = "openai_compatible"

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        endpoint = (
            self.base_url
            if self.base_url.endswith("/embeddings")
            else self.base_url + "/embeddings"
        )
        if not texts:
            return []
        response = None
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                headers = {
                    "X-Aituge-Model-Component-ID": self.registration_id,
                    "X-Request-ID": f"legal-evidence-embedding-{uuid.uuid4()}",
                }
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                response = httpx.post(
                    endpoint,
                    headers=headers,
                    json={
                        "model": self.model,
                        "input": [text[: self.maximum_input_chars] for text in texts],
                        "dimensions": self.dimensions,
                        "encoding_format": "float",
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                retryable = (
                    exc.response.status_code in {408, 429}
                    or exc.response.status_code >= 500
                )
                if not retryable or attempt == self.maximum_attempts:
                    raise
            except httpx.TransportError:
                if attempt == self.maximum_attempts:
                    raise
            time.sleep(self.retry_base_seconds * (2 ** (attempt - 1)))
        if response is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Embedding request produced no response")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Embedding response count mismatch")
        by_index: dict[int, list[float]] = {}
        for item in data:
            index = item.get("index")
            vector = item.get("embedding")
            if not isinstance(index, int) or not isinstance(vector, list):
                raise TypeError("Embedding response item is invalid")
            values = [float(value) for value in vector]
            if len(values) != self.dimensions or not all(
                math.isfinite(value) for value in values
            ):
                raise ValueError("Embedding dimensions or values are invalid")
            by_index[index] = values
        if set(by_index) != set(range(len(texts))):
            raise ValueError("Embedding response indexes are incomplete")
        return [by_index[index] for index in range(len(texts))]
