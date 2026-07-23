from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from proof.application.conflict_retrieval.models import (
    ConflictRetrievalLimits,
    ConflictRetrievalResult,
)
from proof.application.short_refs import attach_short_refs
from proof.errors import ProofError


BRANCH_ORDER = ("same_title", "leaf_category", "parent_category", "global")
CONFLICT_RERANK_INSTRUCTION = (
    "给定一条待审制度规则，优先排序约束同一业务事项、适用范围重叠，"
    "且可能在数值、期限、权限、职责、流程或允许/禁止关系上不一致的制度条款。"
    "不要只按表面词汇相似度排序。"
)


class ConflictRetrievalService:
    """Retrieve cross-policy conflict evidence through metadata-aware vector paths."""

    def __init__(
        self,
        *,
        repository,
        embedding_client,
        reranker=None,
        limits: ConflictRetrievalLimits | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_client = embedding_client
        self.reranker = reranker
        self.limits = limits or ConflictRetrievalLimits()

    def retrieve_for_unit(self, unit_id: str, top_k: int = 10) -> ConflictRetrievalResult:
        source = self.repository.get_conflict_source_unit(unit_id)
        requested_unit_id = unit_id
        if source is None:
            resolver = getattr(self.repository, "resolve_unique_near_unit_id", None)
            corrected_unit_id = resolver(unit_id) if callable(resolver) else None
            if corrected_unit_id:
                unit_id = corrected_unit_id
                source = self.repository.get_conflict_source_unit(unit_id)
        if source is None:
            raise ProofError("retrieval_unit_not_found", "Retrieval unit not found.", status_code=404)
        query = str(source.get("text") or "").strip()
        if not query:
            raise ProofError("empty_query", "Source retrieval unit is empty.", status_code=422)

        query_vector = self.embedding_client.embed([query])[0]
        parent_context = self.repository.get_category_context(str(source.get("category_code") or ""))
        same_title_policy_ids = self.repository.find_effective_policy_ids_by_normalized_title(
            str(source.get("normalized_title") or ""),
            exclude_policy_id=str(source["policy_id"]),
        )
        specs, skipped = self._branch_specs(
            source=source,
            same_title_policy_ids=same_title_policy_ids,
            category_context=parent_context,
        )
        branch_results, failures = self._run_branches(
            specs,
            query_vector,
            excluded_policy_id=str(source["policy_id"]),
        )
        if not branch_results and failures:
            raise ProofError(
                "conflict_retrieval_unavailable",
                "Conflict retrieval is temporarily unavailable.",
                status_code=503,
                details={"reasons": [_failure_reason(name, exc) for name, exc in failures.items()]},
            )

        candidates = _merge_candidates(branch_results, limit=self.limits.max_candidates)
        degradation_reasons = [_failure_reason(name, exc) for name, exc in failures.items()]
        requested_top_k = max(1, min(int(top_k or 10), self.limits.max_candidates))
        reranker_used = False
        reranker_model: str | None = None
        non_same_title_candidates = [
            item for item in candidates if "same_title" not in item.get("retrieval_sources", [])
        ]
        ranked = _select_reranked_evidence(
            candidates,
            non_same_title_candidates,
            limit=requested_top_k,
        )
        if self.reranker is not None and non_same_title_candidates:
            try:
                scores = self.reranker.rerank(
                    query,
                    non_same_title_candidates,
                    top_n=len(non_same_title_candidates),
                )
                reranked = _apply_rerank(non_same_title_candidates, scores)
                if len(reranked) != len(non_same_title_candidates):
                    raise ProofError(
                        "reranker_invalid_response",
                        "Reranker returned an incomplete result set.",
                        status_code=502,
                    )
                ranked = _select_reranked_evidence(
                    candidates,
                    reranked,
                    limit=requested_top_k,
                )
                reranker_used = True
                reranker_model = str(self.reranker.model)
            except Exception as exc:
                degradation_reasons.append(_failure_reason("reranker", exc))
        elif self.reranker is None and non_same_title_candidates:
            degradation_reasons.append("reranker:unconfigured")

        results = attach_short_refs(
            (_with_citation(item) for item in ranked[:requested_top_k]),
            prefix="C",
        )
        source_payload = _with_citation(
            {
                **source,
                "requested_unit_id": requested_unit_id,
                "unit_id_corrected": requested_unit_id != unit_id,
            }
        )
        return ConflictRetrievalResult(
            source=source_payload,
            results=results,
            candidate_counts={
                **{name: len(branch_results.get(name, [])) for name in BRANCH_ORDER},
                "deduplicated": len(candidates),
                "returned": len(results),
            },
            branch_metadata={
                "normalized_title": source.get("normalized_title") or "",
                "leaf_category_code": source.get("category_code"),
                "parent_category_code": parent_context.get("parent_code") if parent_context else None,
                "parent_category_children": (
                    parent_context.get("parent_category_codes", []) if parent_context else []
                ),
            },
            reranker_used=reranker_used,
            reranker_model=reranker_model,
            degraded=bool(degradation_reasons),
            degradation_reasons=degradation_reasons,
            skipped_branches=skipped,
        )

    def _branch_specs(
        self,
        *,
        source: dict[str, Any],
        same_title_policy_ids: list[str],
        category_context: dict[str, Any] | None,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        specs: dict[str, dict[str, Any]] = {}
        skipped: list[str] = []
        if source.get("normalized_title") and same_title_policy_ids:
            specs["same_title"] = {
                "top_k": self.limits.same_title,
                "policy_ids": same_title_policy_ids,
                "category_codes": [],
                "excluded_policy_ids": [],
            }
        else:
            skipped.append("same_title:no_matching_policy")

        category_code = str(source.get("category_code") or "")
        if category_code and category_code != "other" and category_context:
            if int(category_context.get("level") or 0) == 2:
                specs["leaf_category"] = {
                    "top_k": self.limits.leaf_category,
                    "policy_ids": [],
                    "category_codes": [category_code],
                    "excluded_policy_ids": same_title_policy_ids,
                }
                parent_codes = list(category_context.get("parent_category_codes") or [])
                if parent_codes:
                    specs["parent_category"] = {
                        "top_k": self.limits.parent_category,
                        "policy_ids": [],
                        "category_codes": parent_codes,
                        "excluded_policy_ids": same_title_policy_ids,
                    }
                else:
                    skipped.append("parent_category:no_parent")
            else:
                child_codes = list(category_context.get("child_category_codes") or [])
                if child_codes:
                    specs["parent_category"] = {
                        "top_k": self.limits.parent_category,
                        "policy_ids": [],
                        "category_codes": child_codes,
                        "excluded_policy_ids": same_title_policy_ids,
                    }
                skipped.append("leaf_category:source_is_parent")
        else:
            skipped.extend(("leaf_category:no_leaf_category", "parent_category:no_parent"))

        specs["global"] = {
            "top_k": self.limits.global_recall,
            "policy_ids": [],
            "category_codes": [],
            "excluded_policy_ids": same_title_policy_ids,
        }
        return specs, skipped

    def _run_branches(
        self,
        specs: dict[str, dict[str, Any]],
        query_vector: list[float],
        *,
        excluded_policy_id: str,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Exception]]:
        results: dict[str, list[dict[str, Any]]] = {}
        failures: dict[str, Exception] = {}

        def search(spec: dict[str, Any]) -> list[dict[str, Any]]:
            excluded_policy_ids = list(
                dict.fromkeys([excluded_policy_id, *spec.get("excluded_policy_ids", [])])
            )
            return self.repository.vector_search(
                query_vector=query_vector,
                profile=self.embedding_client.profile,
                top_k=spec["top_k"],
                policy_ids=spec["policy_ids"],
                level_codes=[],
                category_codes=spec["category_codes"],
                excluded_policy_ids=excluded_policy_ids,
            )

        with ThreadPoolExecutor(
            max_workers=max(1, len(specs)),
            thread_name_prefix="proof-conflict-retrieval",
        ) as executor:
            futures = {executor.submit(search, spec): name for name, spec in specs.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    failures[name] = exc
        return results, failures


def _merge_candidates(
    branch_results: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for branch in BRANCH_ORDER:
        for rank, raw in enumerate(branch_results.get(branch, []), start=1):
            key = str(raw.get("text_hash") or raw.get("id"))
            if not key:
                continue
            item = merged.get(key)
            if item is None:
                if len(merged) >= limit:
                    continue
                item = {
                    **raw,
                    "retrieval_sources": [],
                    "branch_ranks": {},
                    "branch_scores": {},
                }
                merged[key] = item
            if branch not in item["retrieval_sources"]:
                item["retrieval_sources"].append(branch)
            item["branch_ranks"][branch] = rank
            item["branch_scores"][branch] = float(raw.get("score") or 0.0)
    return list(merged.values())


def _apply_rerank(candidates: list[dict[str, Any]], scores) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rank, score in enumerate(scores, start=1):
        index = int(score.index)
        if index < 0 or index >= len(candidates) or index in seen:
            continue
        seen.add(index)
        item = {**candidates[index]}
        item["rerank_rank"] = rank
        item["rerank_score"] = float(score.score)
        item["score"] = float(score.score)
        ranked.append(item)
    if not ranked:
        raise ProofError(
            "reranker_invalid_response",
            "Reranker returned no usable results.",
            status_code=502,
        )
    return ranked


def _select_reranked_evidence(
    candidates: list[dict[str, Any]],
    reranked: list[dict[str, Any]],
    *,
    limit: int,
    same_title_limit: int = 6,
) -> list[dict[str, Any]]:
    """Keep normalized-title evidence, then fill from reranked non-title candidates."""

    reranked_by_key = {
        str(item.get("text_hash") or item.get("id")): item
        for item in reranked
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(raw: dict[str, Any]) -> bool:
        key = str(raw.get("text_hash") or raw.get("id"))
        if not key or key in seen or len(selected) >= limit:
            return False
        seen.add(key)
        selected.append(reranked_by_key.get(key, raw))
        return True

    same_title_candidates = [
        item for item in candidates if "same_title" in item.get("retrieval_sources", [])
    ]
    same_title_candidates.sort(
        key=lambda item: int((item.get("branch_ranks") or {}).get("same_title") or 10_000)
    )
    same_title_limit = max(0, min(same_title_limit, limit))
    for representative in same_title_candidates[:same_title_limit]:
        add(representative)

    for candidate in reranked:
        if len(selected) >= limit:
            break
        if "same_title" in candidate.get("retrieval_sources", []):
            continue
        add(candidate)
    return selected


def _with_citation(item: dict[str, Any]) -> dict[str, Any]:
    clause_label = item.get("clause_no_raw") or "未编号条款"
    citation_label = (
        f"[{item.get('policy_title') or '未知制度'}｜{clause_label}｜"
        f"Chunk #{item.get('clause_ordinal')}]"
    )
    return {
        **item,
        "citation": {
            "policy_id": item.get("policy_id"),
            "policy_title": item.get("policy_title"),
            "policy_version": item.get("policy_version"),
            "document_id": item.get("document_id"),
            "original_name": item.get("original_name"),
            "clause_no_raw": item.get("clause_no_raw"),
            "clause_ordinal": item.get("clause_ordinal"),
            "heading_path": item.get("heading_path") or [],
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "label": citation_label,
        },
    }


def _failure_reason(name: str, exc: Exception) -> str:
    if isinstance(exc, ProofError):
        return f"{name}:{exc.code}"
    return f"{name}:{exc.__class__.__name__}"
