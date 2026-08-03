from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.model_gateway import gateway_headers, raise_gateway_error
from proof.infrastructure.model_observability import (
    ProofInvocationObserver,
    ProofModelInvocationObserver,
    usage_token_counts,
    new_logical_call_id,
)
from proof.model_runtime import EmbeddingRuntimeConfig, build_proof_model_runtime


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    id: str
    provider: str
    model: str
    dimensions: int


class _EmbeddingDimensionMismatch(ValueError):
    pass


class OpenAICompatibleEmbeddingClient:
    batch_size = 10

    def __init__(
        self,
        config: EmbeddingRuntimeConfig | Settings,
        *,
        observer: ProofInvocationObserver | None = None,
    ) -> None:
        observer_settings = config if isinstance(config, Settings) else None
        if isinstance(config, Settings):
            config = build_proof_model_runtime(config).embedding
        if config is None:
            raise ProofError(
                "embedding_unconfigured",
                "Embedding API is not configured.",
                status_code=503,
            )
        self.registration_id = config.id
        self.mode = config.mode
        self.provider = config.provider
        self.base_url = config.base_url.rstrip("/")
        self.api_key = config.api_key
        self.model = config.model
        self.dimensions = config.dimensions
        self.timeout = config.timeout_seconds
        self.via_gateway = config.via_gateway
        self.observer = observer or ProofModelInvocationObserver.from_settings(
            observer_settings
        )
        # Preserve the established profile algorithm so moving the same model
        # into the registry does not invalidate existing pgvector rows.
        identity_base_url = config.identity_base_url or self.base_url
        profile_source = f"openai_compatible|{identity_base_url.rstrip('/')}|{self.model}|{self.dimensions}"
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
        endpoint = (
            self.base_url
            if self.base_url.endswith("/embeddings")
            else f"{self.base_url}/embeddings"
        )
        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        logical_call_id = new_logical_call_id()
        fallback_from_invocation_id: str | None = None
        last_error = ""
        attempts = 1 if self.via_gateway else 3
        for attempt_index in range(attempts):
            attempt_no = attempt_index + 1
            observation = self.observer.begin(
                feature_code="proof.embedding",
                provider=self.provider,
                model_name=self.model,
                mode=self.mode,
                registration_id=self.registration_id,
                logical_call_id=logical_call_id,
                attempt_no=attempt_no,
                fallback_from_invocation_id=fallback_from_invocation_id,
            )
            try:
                response = httpx.post(
                    endpoint,
                    headers=gateway_headers(
                        self.api_key,
                        self.registration_id,
                        via_gateway=self.via_gateway,
                    ),
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
                observation.transport_failed(
                    "PROOF_EMBEDDING_TRANSPORT_ERROR",
                    retry_reason=(
                        "PROVIDER_RETRY" if attempt_index < attempts - 1 else None
                    ),
                )
                fallback_from_invocation_id = observation.invocation_id
                if attempt_index < attempts - 1:
                    time.sleep(0.5 * (2**attempt_index))
                    continue
                raise ProofError(
                    "embedding_unavailable",
                    "Embedding API request failed.",
                    status_code=503,
                    details={"reason": last_error},
                ) from exc

            observation.dispatched(response)
            if self.via_gateway and response.status_code >= 400:
                observation.failed("PROOF_EMBEDDING_GATEWAY_REJECTED")
                raise_gateway_error(response)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                observation.failed(
                    "PROOF_EMBEDDING_PROVIDER_UNAVAILABLE",
                    retry_reason=(
                        "PROVIDER_RETRY" if attempt_index < attempts - 1 else None
                    ),
                )
                fallback_from_invocation_id = observation.invocation_id
                if attempt_index < attempts - 1:
                    time.sleep(0.5 * (2**attempt_index))
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
                if response.status_code == 413 or any(
                    marker in error_text for marker in too_long_markers
                ):
                    observation.failed("PROOF_EMBEDDING_INPUT_TOO_LONG")
                    raise ProofError(
                        "embedding_too_long",
                        "Embedding API cannot accept this complete clause.",
                        status_code=422,
                        details={"status_code": response.status_code},
                    )
                observation.failed("PROOF_EMBEDDING_REQUEST_REJECTED")
                raise ProofError(
                    "embedding_request_rejected",
                    "Embedding API rejected the request.",
                    status_code=422,
                    details={"status_code": response.status_code},
                )
            try:
                body = response.json()
                vectors = _validate_embedding_response(
                    body,
                    expected_count=len(texts),
                    dimensions=self.dimensions,
                )
                input_tokens, output_tokens = usage_token_counts(
                    body.get("usage") or {}
                )
            except _EmbeddingDimensionMismatch as exc:
                observation.validation_failed(
                    "PROOF_EMBEDDING_DIMENSION_MISMATCH"
                )
                raise ProofError(
                    "embedding_dimension_mismatch",
                    "Embedding API returned a vector with the wrong dimension.",
                    status_code=502,
                ) from exc
            except (
                AttributeError,
                KeyError,
                OverflowError,
                TypeError,
                ValueError,
            ) as exc:
                observation.validation_failed("PROOF_EMBEDDING_INVALID_RESPONSE")
                raise ProofError(
                    "embedding_invalid_response",
                    "Embedding API returned an invalid response.",
                    status_code=502,
                ) from exc
            observation.succeeded(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            return vectors
        raise AssertionError("unreachable")


def _validate_embedding_response(
    body: Any,
    *,
    expected_count: int,
    dimensions: int,
) -> list[list[float]]:
    if not isinstance(body, dict):
        raise TypeError("embedding response must be an object")
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise ValueError("embedding response count mismatch")
    vectors_by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise TypeError("embedding item must be an object")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("embedding index must be an integer")
        if index < 0 or index >= expected_count or index in vectors_by_index:
            raise ValueError("embedding index is invalid")
        raw_vector = item.get("embedding")
        if not isinstance(raw_vector, list) or len(raw_vector) != dimensions:
            raise _EmbeddingDimensionMismatch("embedding dimension mismatch")
        vector: list[float] = []
        for value in raw_vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("embedding value must be numeric")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("embedding value must be finite")
            vector.append(number)
        vectors_by_index[index] = vector
    if set(vectors_by_index) != set(range(expected_count)):
        raise ValueError("embedding indexes are incomplete")
    return [vectors_by_index[index] for index in range(expected_count)]
