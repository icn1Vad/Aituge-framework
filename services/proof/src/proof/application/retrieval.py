from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any, Protocol

from proof.errors import ProofError


RETRIEVAL_MODES = {"hybrid", "vector", "keyword"}


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    policy_ids: list[str] = field(default_factory=list)
    level_codes: list[str] = field(default_factory=list)
    category_codes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalBranchResult:
    items: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RerankScore:
    index: int
    score: float


class PolicyRetriever(Protocol):
    name: str

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        filters: RetrievalFilters,
    ) -> RetrievalBranchResult: ...


class PolicyReranker(Protocol):
    model: str

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        *,
        top_n: int,
    ) -> list[RerankScore]: ...


class HybridPolicyRetriever:
    """Coordinate independent recall paths and one optional ranking stage."""

    def __init__(
        self,
        *,
        keyword: PolicyRetriever,
        vector: PolicyRetriever,
        reranker: PolicyReranker | None,
        keyword_limit: int = 30,
        vector_limit: int = 30,
        rerank_candidate_limit: int = 30,
    ) -> None:
        self.keyword = keyword
        self.vector = vector
        self.reranker = reranker
        self.keyword_limit = keyword_limit
        self.vector_limit = vector_limit
        self.rerank_candidate_limit = rerank_candidate_limit

    def search(
        self,
        query: str,
        *,
        top_k: int,
        mode: str,
        filters: RetrievalFilters,
    ) -> dict[str, Any]:
        mode = mode.strip().lower()
        if mode not in RETRIEVAL_MODES:
            raise ProofError(
                "invalid_retrieval_mode",
                f"Unsupported retrieval mode: {mode}",
                status_code=422,
            )

        branches = self._run_branches(query, mode=mode, filters=filters)
        successful = {name: result for name, result in branches.items() if isinstance(result, RetrievalBranchResult)}
        failures = {name: result for name, result in branches.items() if isinstance(result, Exception)}
        if not successful:
            raise ProofError(
                "retrieval_unavailable",
                "Policy retrieval is temporarily unavailable.",
                status_code=503,
                details={"reasons": [_failure_reason(name, error) for name, error in failures.items()]},
            )

        candidates = _fuse_candidates(successful)
        deduplicated_count = len(candidates)
        reasons = [_failure_reason(name, error) for name, error in failures.items()]
        reranker_used = False
        if self.reranker is not None and candidates:
            rerank_input = candidates[: self.rerank_candidate_limit]
            try:
                scores = self.reranker.rerank(
                    query,
                    rerank_input,
                    top_n=min(top_k, len(rerank_input)),
                )
                candidates = _apply_rerank_scores(rerank_input, scores)
                reranker_used = True
            except Exception as exc:  # the recall result remains usable
                reasons.append(_failure_reason("reranker", exc))
        elif self.reranker is None:
            reasons.append("reranker_unconfigured")

        results = candidates[:top_k]
        return {
            "query": query,
            "retrieval_mode": mode,
            "reranker_used": reranker_used,
            "reranker_model": self.reranker.model if reranker_used and self.reranker else None,
            "degraded": bool(reasons),
            "degradation_reasons": reasons,
            "candidate_counts": {
                **{
                    name: len(result.items)
                    for name, result in successful.items()
                },
                "deduplicated": deduplicated_count,
                "returned": len(results),
            },
            "branch_metadata": {
                name: result.metadata
                for name, result in successful.items()
                if result.metadata
            },
            "results": results,
        }

    def _run_branches(
        self,
        query: str,
        *,
        mode: str,
        filters: RetrievalFilters,
    ) -> dict[str, RetrievalBranchResult | Exception]:
        selected: list[tuple[PolicyRetriever, int]] = []
        if mode in {"hybrid", "keyword"}:
            selected.append((self.keyword, self.keyword_limit))
        if mode in {"hybrid", "vector"}:
            selected.append((self.vector, self.vector_limit))

        if len(selected) == 1:
            retriever, limit = selected[0]
            try:
                return {retriever.name: retriever.retrieve(query, limit=limit, filters=filters)}
            except Exception as exc:
                return {retriever.name: exc}

        results: dict[str, RetrievalBranchResult | Exception] = {}
        with ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="proof-retrieval") as executor:
            futures = {
                executor.submit(
                    copy_context().run,
                    retriever.retrieve,
                    query,
                    limit=limit,
                    filters=filters,
                ): retriever.name
                for retriever, limit in selected
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = exc
        return results


def _fuse_candidates(
    branches: dict[str, RetrievalBranchResult],
) -> list[dict[str, Any]]:
    branch_scores = {
        name: _normalize_scores(result.items)
        for name, result in branches.items()
    }
    merged: dict[str, dict[str, Any]] = {}
    for name in ("keyword", "vector"):
        result = branches.get(name)
        if result is None:
            continue
        normalized = branch_scores[name]
        for index, item in enumerate(result.items):
            key = str(item.get("text_hash") or item.get("id"))
            candidate = merged.setdefault(
                key,
                {
                    **item,
                    "keyword_score": None,
                    "vector_score": None,
                    "retrieval_sources": [],
                },
            )
            raw_score = float(item.get("score") or 0.0)
            existing = candidate.get(f"{name}_score")
            if existing is None or raw_score > float(existing):
                candidate[f"{name}_score"] = raw_score
                candidate[f"{name}_normalized_score"] = normalized[index]
            if name not in candidate["retrieval_sources"]:
                candidate["retrieval_sources"].append(name)

    active_names = [name for name in ("keyword", "vector") if name in branches]
    weight = 1.0 / len(active_names)
    for candidate in merged.values():
        candidate["fused_score"] = sum(
            weight * float(candidate.get(f"{name}_normalized_score") or 0.0)
            for name in active_names
        )
        candidate["score"] = candidate["fused_score"]
    return sorted(merged.values(), key=lambda item: float(item["fused_score"]), reverse=True)


def _normalize_scores(items: list[dict[str, Any]]) -> list[float]:
    if not items:
        return []
    scores = [float(item.get("score") or 0.0) for item in items]
    low, high = min(scores), max(scores)
    if high == low:
        return [1.0 for _ in scores]
    return [(score - low) / (high - low) for score in scores]


def _apply_rerank_scores(
    candidates: list[dict[str, Any]],
    scores: list[RerankScore],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for result in scores:
        if result.index < 0 or result.index >= len(candidates) or result.index in seen:
            continue
        seen.add(result.index)
        candidate = {**candidates[result.index]}
        candidate["rerank_score"] = result.score
        candidate["score"] = result.score
        ranked.append(candidate)
    if not ranked:
        raise ProofError(
            "reranker_invalid_response",
            "Reranker returned no usable results.",
            status_code=502,
        )
    return ranked


def _failure_reason(name: str, error: Exception) -> str:
    code = error.code if isinstance(error, ProofError) else type(error).__name__
    return f"{name}:{code}"
