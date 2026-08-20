from __future__ import annotations

import concurrent.futures
import re
from collections import defaultdict
from typing import Any

from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.model_clients import ModelClients
from qianxuesen_mentor.repository import Repository


DATE_INTENT = re.compile(r"哪年|何时|什么时候|日期|出生|回国|逝世|\d{4}年")
QUOTE_INTENT = re.compile(r"原话|原文|怎么说|出处|引用|哪一页")
METHOD_INTENT = re.compile(r"方法|思想|原则|系统工程|工程控制论|总体设计|怎么做|如何")


class RetrievalService:
    def __init__(self, settings: Settings, repository: Repository, models: ModelClients | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self.models = models

    def search(self, query: str, *, top_k: int, retrieval_mode: str) -> dict[str, Any]:
        quotas = route_quotas(query, top_k)
        degradations: list[dict[str, str]] = []
        query_embedding: list[float] | None = None
        if retrieval_mode in {"hybrid", "vector"} and self.models:
            try:
                query_embedding = self.models.embed([query])[0]
            except Exception as exc:
                degradations.append({"component": "embedding", "reason": type(exc).__name__})
        elif retrieval_mode == "vector":
            degradations.append({"component": "embedding", "reason": "unconfigured"})

        jobs: dict[tuple[str, str], Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for layer in ("fact", "principle", "book"):
                if retrieval_mode in {"hybrid", "keyword"}:
                    jobs[(layer, "keyword")] = pool.submit(
                        self.repository.keyword_search, layer, query, self.settings.retrieval_keyword_limit
                    )
                if query_embedding is not None and self.models and retrieval_mode in {"hybrid", "vector"}:
                    jobs[(layer, "vector")] = pool.submit(
                        self.repository.vector_search, layer, query_embedding,
                        self.models.profile_id, self.settings.retrieval_vector_limit,
                    )
            branch_results: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for key, future in jobs.items():
                try:
                    branch_results[key] = future.result()
                except Exception as exc:
                    branch_results[key] = []
                    degradations.append({"component": f"{key[0]}_{key[1]}", "reason": type(exc).__name__})

        selected: list[dict[str, Any]] = []
        layer_counts: dict[str, int] = {}
        for layer in ("fact", "principle", "book"):
            fused = reciprocal_rank_fusion([
                branch_results.get((layer, "keyword"), []), branch_results.get((layer, "vector"), [])
            ])
            chosen = fused[:quotas[layer]]
            layer_counts[layer] = len(chosen)
            selected.extend(chosen)
        if self.models and len(selected) > 1:
            try:
                selected = self.models.rerank(query, selected[:self.settings.rerank_candidate_limit], len(selected))
            except Exception as exc:
                degradations.append({"component": "reranker", "reason": type(exc).__name__})
                selected.sort(key=lambda item: item.get("fusion_score", 0), reverse=True)
        else:
            selected.sort(key=lambda item: item.get("fusion_score", 0), reverse=True)
        return {
            "query": query, "retrieval_mode": retrieval_mode, "routing": quotas,
            "results": [evidence_view(item) for item in selected[:top_k]],
            "layer_counts": layer_counts, "degraded": bool(degradations), "degradations": degradations,
        }


def route_quotas(query: str, top_k: int) -> dict[str, int]:
    weights = {"fact": 3, "principle": 3, "book": 6}
    if DATE_INTENT.search(query):
        weights = {"fact": 5, "principle": 1, "book": 6}
    elif QUOTE_INTENT.search(query):
        weights = {"fact": 1, "principle": 1, "book": 10}
    elif METHOD_INTENT.search(query):
        weights = {"fact": 1, "principle": 5, "book": 6}
    total = sum(weights.values())
    quotas = {key: max(1, round(top_k * value / total)) for key, value in weights.items()}
    while sum(quotas.values()) > top_k:
        key = max(quotas, key=lambda item: quotas[item] / weights[item])
        if quotas[key] > 1:
            quotas[key] -= 1
        else:
            break
    while sum(quotas.values()) < top_k:
        quotas[max(weights, key=weights.get)] += 1
    return quotas


def reciprocal_rank_fusion(rankings: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    scores: dict[str, float] = defaultdict(float)
    items: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            item_id = str(item["id"])
            scores[item_id] += 1 / (k + rank)
            items[item_id] = dict(item)
    output = []
    for item_id, score in scores.items():
        item = items[item_id]
        item["fusion_score"] = score
        output.append(item)
    return sorted(output, key=lambda item: item["fusion_score"], reverse=True)


def evidence_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_type": item["evidence_type"], "document_id": item["document_id"],
        "document_name": item["document_name"], "chapter": item.get("title") or "",
        "page_start": item["page_start"], "page_end": item["page_end"],
        "chunk_id": item["chunk_id"], "quote": item["content"],
        "confidence": float(item.get("confidence") or 0), "card_status": item.get("card_status"),
        "retrieval_score": float(item.get("rerank_score", item.get("fusion_score", 0))),
        "citation": f"《{item['document_name']}》第{item['page_start']}页",
    }
