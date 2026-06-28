"""PAI-style fusion and optional rerank for retrieval results."""

from __future__ import annotations

from .models import RetrievalResult
from .rerank import Reranker


def filter_results(
    results: list[RetrievalResult],
    *,
    similarity_threshold: float = 0.0,
    top_k: int | None = None,
) -> list[RetrievalResult]:
    filtered = [
        result
        for result in results
        if result.score >= similarity_threshold
    ]
    filtered.sort(key=lambda item: item.score, reverse=True)
    return filtered[:top_k] if top_k else filtered


def weight_fusion(
    text_results: list[RetrievalResult],
    dense_results: list[RetrievalResult],
    *,
    vector_weight: float,
    similarity_threshold: float = 0.0,
    top_k: int,
) -> list[RetrievalResult]:
    """Merge text and dense candidates with PAI's weighted-score idea."""

    text_weight = 1 - vector_weight
    merged: dict[str, RetrievalResult] = {}
    scores: dict[str, float] = {}

    for result in text_results:
        key = result.chunk_id
        merged[key] = result
        scores[key] = scores.get(key, 0.0) + result.score * text_weight

    for result in dense_results:
        key = result.chunk_id
        merged.setdefault(key, result)
        scores[key] = scores.get(key, 0.0) + result.score * vector_weight

    fused = [
        RetrievalResult(
            content=result.content,
            score=scores[key],
            kb_id=result.kb_id,
            chunk_id=result.chunk_id,
            file_id=result.file_id,
            title=result.title,
            file_name=result.file_name,
            url=result.url,
            metadata=result.metadata,
        )
        for key, result in merged.items()
        if scores[key] >= similarity_threshold
    ]
    fused.sort(key=lambda item: item.score, reverse=True)
    return fused[:top_k]


async def arerank_fusion(
    *,
    query: str,
    text_results: list[RetrievalResult] | None = None,
    dense_results: list[RetrievalResult] | None = None,
    rerank_model: Reranker | None = None,
    vector_weight: float = 0.5,
    top_k: int = 5,
    rerank_top_k: int = 5,
    similarity_threshold: float = 0.0,
) -> list[RetrievalResult]:
    """Mirror PAI-RAG's branch structure for no-rerank vs model-rerank."""

    text_results = text_results or []
    dense_results = dense_results or []

    if not text_results:
        candidates = filter_results(
            dense_results,
            similarity_threshold=similarity_threshold,
            top_k=top_k,
        )
    elif not dense_results:
        candidates = filter_results(
            text_results,
            similarity_threshold=similarity_threshold,
            top_k=top_k,
        )
    elif rerank_model is None:
        return weight_fusion(
            text_results,
            dense_results,
            vector_weight=vector_weight,
            similarity_threshold=similarity_threshold,
            top_k=top_k,
        )
    else:
        deduped = {result.content: result for result in [*text_results, *dense_results]}
        candidates = list(deduped.values())

    if rerank_model is None:
        return candidates[:top_k]

    return await rerank_model.rerank(
        query,
        candidates,
        top_n=rerank_top_k,
        similarity_threshold=similarity_threshold,
    )

