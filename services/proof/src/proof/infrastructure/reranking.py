from __future__ import annotations

import time
import math
from typing import Any

import httpx

from proof.application.retrieval import RerankScore
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.model_gateway import gateway_headers, raise_gateway_error
from proof.infrastructure.model_observability import (
    ProofAttemptObserver,
    ProofInvocationObserver,
    ProofModelInvocationObserver,
    new_logical_call_id,
    usage_token_counts,
)
from proof.model_runtime import RerankerRuntimeConfig, build_proof_model_runtime


class OpenAICompatiblePolicyReranker:
    """Client for API or local OpenAI-compatible rerank endpoints."""

    def __init__(
        self,
        config: RerankerRuntimeConfig | Settings,
        *,
        instruction: str | None = None,
        observer: ProofInvocationObserver | None = None,
    ) -> None:
        default_instruction = ""
        observer_settings = config if isinstance(config, Settings) else None
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
        self.provider = config.provider
        self.endpoint = config.base_url.rstrip("/")
        if not self.endpoint.endswith(("/rerank", "/reranks")):
            self.endpoint += "/reranks"
        self.api_key = config.api_key
        self.model = config.model.strip()
        self.timeout = config.timeout_seconds
        self.instruction = (instruction or default_instruction).strip()
        self.via_gateway = config.via_gateway
        self.observer = observer or ProofModelInvocationObserver.from_settings(
            observer_settings
        )

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
        response, observation = self._post(payload)
        try:
            body = response.json()
            results = _validate_reranker_response(
                body,
                candidate_count=len(candidates),
                expected_count=min(top_n, len(candidates)),
            )
            input_tokens, output_tokens = usage_token_counts(
                body.get("usage") or {}
            )
        except (
            AttributeError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
        ) as exc:
            observation.validation_failed("PROOF_RERANKER_INVALID_RESPONSE")
            raise ProofError(
                "reranker_invalid_response",
                "Reranker returned an invalid response.",
                status_code=502,
            ) from exc
        observation.succeeded(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return results

    def _post(
        self,
        payload: dict[str, Any],
    ) -> tuple[Any, ProofAttemptObserver]:
        logical_call_id = new_logical_call_id()
        fallback_from_invocation_id: str | None = None
        last_reason = ""
        attempts = 1 if self.via_gateway else 3
        for attempt_index in range(attempts):
            observation = self.observer.begin(
                feature_code="proof.reranker",
                provider=self.provider,
                model_name=self.model,
                mode=self.mode,
                registration_id=self.registration_id,
                logical_call_id=logical_call_id,
                attempt_no=attempt_index + 1,
                fallback_from_invocation_id=fallback_from_invocation_id,
            )
            try:
                response = httpx.post(
                    self.endpoint,
                    headers=gateway_headers(
                        self.api_key,
                        self.registration_id,
                        via_gateway=self.via_gateway,
                    ),
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                last_reason = type(exc).__name__
                observation.transport_failed(
                    "PROOF_RERANKER_TRANSPORT_ERROR",
                    retry_reason=(
                        "PROVIDER_RETRY" if attempt_index < attempts - 1 else None
                    ),
                )
                fallback_from_invocation_id = observation.invocation_id
                if attempt_index < attempts - 1:
                    time.sleep(0.5 * (2**attempt_index))
                    continue
                raise ProofError(
                    "reranker_unavailable",
                    "Reranker API request failed.",
                    status_code=503,
                    details={"reason": last_reason},
                ) from exc
            observation.dispatched(response)
            if self.via_gateway and response.status_code >= 400:
                observation.failed("PROOF_RERANKER_GATEWAY_REJECTED")
                raise_gateway_error(response)
            if response.status_code == 429 or response.status_code >= 500:
                last_reason = f"HTTP {response.status_code}"
                observation.failed(
                    "PROOF_RERANKER_PROVIDER_UNAVAILABLE",
                    retry_reason=(
                        "PROVIDER_RETRY" if attempt_index < attempts - 1 else None
                    ),
                )
                fallback_from_invocation_id = observation.invocation_id
                if attempt_index < attempts - 1:
                    time.sleep(0.5 * (2**attempt_index))
                    continue
                raise ProofError(
                    "reranker_unavailable",
                    "Reranker API is temporarily unavailable.",
                    status_code=503,
                    details={"reason": last_reason},
                )
            if response.status_code >= 400:
                observation.failed("PROOF_RERANKER_REQUEST_REJECTED")
                raise ProofError(
                    "reranker_request_rejected",
                    "Reranker API rejected the request.",
                    status_code=502,
                    details={"status_code": response.status_code},
                )
            return response, observation
        raise AssertionError("unreachable")
def _validate_reranker_response(
    body: Any,
    *,
    candidate_count: int,
    expected_count: int,
) -> list[RerankScore]:
    if not isinstance(body, dict):
        raise TypeError("reranker response must be an object")
    raw_results = body.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != expected_count:
        raise ValueError("reranker result count mismatch")
    seen: set[int] = set()
    results: list[RerankScore] = []
    for item in raw_results:
        if not isinstance(item, dict):
            raise TypeError("reranker item must be an object")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("reranker index must be an integer")
        if index < 0 or index >= candidate_count or index in seen:
            raise ValueError("reranker index is invalid")
        raw_score = item.get("relevance_score")
        if isinstance(raw_score, bool) or not isinstance(
            raw_score,
            (int, float),
        ):
            raise TypeError("reranker score must be numeric")
        score = float(raw_score)
        if not math.isfinite(score):
            raise ValueError("reranker score must be finite")
        seen.add(index)
        results.append(RerankScore(index=index, score=score))
    return results




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
