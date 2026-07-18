from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from proof.application.retrieval import RetrievalBranchResult, _fuse_candidates
from proof.config import Settings
from proof.domain.policy_grouping import infer_coarse_policy_category, infer_policy_category
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.postgres.repository import ProofRepository
from proof.infrastructure.reranking import DashScopePolicyReranker


SAMPLE_IDS = (
    "CF-001",
    "CF-002",
    "CF-003",
    "CF-004",
    "CF-007",
    "CF-008",
    "CF-010",
    "CF-015",
    "CF-016",
    "CF-018",
    "CF-022",
    "CF-023",
    "CF-026",
    "CF-029",
    "CF-030",
    "CF-031",
    "PT-002",
    "PT-003",
    "PT-006",
    "PT-014",
    "PT-015",
    "PT-016",
    "PT-030",
    "PT-035",
)
KS = (3, 5, 8, 10, 20)
CONFLICT_RERANK_INSTRUCTION = (
    "给定一条待审制度规则，优先排序约束同一业务事项、适用范围重叠，且可能在数值、期限、"
    "权限、职责、流程或允许/禁止关系上不一致的制度条款。不要只按表面词汇相似度排序。"
)


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _load_samples(path: Path, *, all_conflicts: bool) -> list[dict[str, Any]]:
    registry = json.loads(path.read_text(encoding="utf-8"))
    if all_conflicts:
        return [item for item in registry if item.get("taxonomy_category") == "冲突类"]
    wanted = set(SAMPLE_IDS)
    return [item for item in registry if item.get("sample_id") in wanted]


def _load_units(repository: ProofRepository) -> list[dict[str, Any]]:
    with repository.connect() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.document_id, u.policy_id, u.text, u.text_hash,
                   u.clause_no_raw, u.clause_ordinal, u.unit_type, u.heading_path,
                   p.title AS policy_title, p.level_code, p.category_code,
                   d.original_name
            FROM proof_retrieval_unit u
            JOIN proof_policy p ON p.id = u.policy_id
            JOIN proof_document d ON d.id = u.document_id
            WHERE p.status = 'effective'
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _match_text(text: str, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    needle = _normalized(text)
    return [unit for unit in units if needle in _normalized(str(unit["text"]))]


def _map_cases(samples: list[dict[str, Any]], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for sample in samples:
        texts = sample["modified_texts"]
        query_text = texts[-1]
        target_texts = texts[:-1]
        query_units = _match_text(query_text, units)
        target_groups = [_match_text(text, units) for text in target_texts]
        cases.append(
            {
                "sample_id": sample["sample_id"],
                "error_type": sample["error_type"],
                "assigned_regulation": sample["assigned_regulation"],
                "query_text": query_text,
                "query_units": query_units,
                "target_texts": target_texts,
                "target_groups": target_groups,
            }
        )
    missing = [
        {
            "sample_id": case["sample_id"],
            "query_matches": len(case["query_units"]),
            "target_matches": [len(group) for group in case["target_groups"]],
        }
        for case in cases
        if not case["query_units"] or any(not group for group in case["target_groups"])
    ]
    if missing:
        raise RuntimeError(f"Could not map benchmark texts to retrieval units: {missing}")
    return cases


def _without_query(items: Iterable[dict[str, Any]], query_ids: set[str]) -> list[dict[str, Any]]:
    return [item for item in items if str(item["id"]) not in query_ids]


def _rrf(
    keyword: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    rank_constant: int = 60,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source, items in (("keyword", keyword), ("vector", vector)):
        for rank, item in enumerate(items, start=1):
            key = str(item.get("text_hash") or item["id"])
            candidate = merged.setdefault(
                key,
                {
                    **item,
                    "retrieval_sources": [],
                    "keyword_score": None,
                    "vector_score": None,
                    "rrf_score": 0.0,
                },
            )
            candidate["rrf_score"] += 1.0 / (rank_constant + rank)
            candidate[f"{source}_rank"] = rank
            candidate[f"{source}_score"] = float(item.get("score") or 0.0)
            if source not in candidate["retrieval_sources"]:
                candidate["retrieval_sources"].append(source)
    for candidate in merged.values():
        candidate["score"] = candidate["rrf_score"]
    return sorted(merged.values(), key=lambda item: (-float(item["rrf_score"]), str(item["id"])))


def _branch_preserving_pool(
    rrf_items: list[dict[str, Any]],
    keyword: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    limit: int,
    branch_quota: int = 15,
) -> list[dict[str, Any]]:
    by_key = {str(item.get("text_hash") or item["id"]): item for item in rrf_items}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(items: Iterable[dict[str, Any]]) -> None:
        for raw in items:
            key = str(raw.get("text_hash") or raw["id"])
            if key in seen or len(selected) >= limit:
                continue
            seen.add(key)
            selected.append(by_key[key])

    add(keyword[:branch_quota])
    add(vector[:branch_quota])
    add(rrf_items)
    return selected


def _merge_pools(*pools: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in pools:
        for item in pool:
            key = str(item.get("text_hash") or item["id"])
            if key in seen or len(merged) >= limit:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _rerank(
    reranker: DashScopePolicyReranker,
    query: str,
    candidates: list[dict[str, Any]],
    *,
    top_n: int,
) -> list[dict[str, Any]]:
    scores = reranker.rerank(query, candidates, top_n=min(top_n, len(candidates)))
    ranked: list[dict[str, Any]] = []
    for score in scores:
        item = {**candidates[score.index]}
        item["rerank_score"] = score.score
        item["score"] = score.score
        ranked.append(item)
    return ranked


def _rank_for_group(items: list[dict[str, Any]], group: list[dict[str, Any]]) -> int | None:
    target_ids = {str(item["id"]) for item in group}
    for rank, item in enumerate(items, start=1):
        if str(item["id"]) in target_ids:
            return rank
    return None


def _summarize(case_results: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    ranks = [row["strategies"][strategy]["best_rank"] for row in case_results]
    group_ranks = [
        rank
        for row in case_results
        for rank in row["strategies"][strategy]["target_group_ranks"]
    ]
    return {
        "case_any_target_recall": {
            f"@{k}": sum(rank is not None and rank <= k for rank in ranks) / len(ranks)
            for k in KS
        },
        "target_group_recall": {
            f"@{k}": sum(rank is not None and rank <= k for rank in group_ranks) / len(group_ranks)
            for k in KS
        },
        "case_all_targets_recall": {
            f"@{k}": sum(
                all(rank is not None and rank <= k for rank in row["strategies"][strategy]["target_group_ranks"])
                for row in case_results
            )
            / len(case_results)
            for k in KS
        },
        "misses_at_10": [
            row["sample_id"]
            for row in case_results
            if row["strategies"][strategy]["best_rank"] is None
            or row["strategies"][strategy]["best_rank"] > 10
        ],
    }


def run(output: Path, *, run_reranker: bool, all_conflicts: bool) -> dict[str, Any]:
    settings = Settings()
    repository = ProofRepository(settings)
    registry_path = _workspace_root() / "数据集/work/tmp/sample_registry.json"
    units = _load_units(repository)
    cases = _map_cases(_load_samples(registry_path, all_conflicts=all_conflicts), units)
    policy_ids_by_title: dict[str, set[str]] = defaultdict(set)
    policy_ids_by_inferred_category: dict[str, set[str]] = defaultdict(set)
    policy_ids_by_group: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        policy_title = str(unit["policy_title"])
        policy_id = str(unit["policy_id"])
        policy_ids_by_title[policy_title].add(policy_id)
        policy_ids_by_inferred_category[infer_coarse_policy_category(policy_title)].add(policy_id)
        policy_ids_by_group[infer_policy_category(policy_title)].add(policy_id)

    embedding_client = OpenAICompatibleEmbeddingClient(settings)
    query_vectors = embedding_client.embed([case["query_text"] for case in cases])
    current_reranker = DashScopePolicyReranker(settings) if run_reranker else None
    improved_settings = settings.model_copy(update={"rerank_instruction": CONFLICT_RERANK_INSTRUCTION})
    improved_reranker = DashScopePolicyReranker(improved_settings) if run_reranker else None

    case_results: list[dict[str, Any]] = []
    for case, query_vector in zip(cases, query_vectors, strict=True):
        query_ids = {str(item["id"]) for item in case["query_units"]}
        keyword = _without_query(
            repository.keyword_search(
                query=case["query_text"],
                top_k=100,
                policy_ids=[],
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        vector = _without_query(
            repository.vector_search(
                query_vector=query_vector,
                profile=embedding_client.profile,
                top_k=100,
                policy_ids=[],
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        current_fusion = _fuse_candidates(
            {
                "keyword": RetrievalBranchResult(keyword[:30]),
                "vector": RetrievalBranchResult(vector[:30]),
            }
        )
        improved_rrf = _rrf(keyword, vector)
        improved_pool = _branch_preserving_pool(improved_rrf, keyword, vector, limit=60)
        query_category_codes = sorted(
            {str(item["category_code"]) for item in case["query_units"]}
        )
        category_keyword = _without_query(
            repository.keyword_search(
                query=case["query_text"],
                top_k=100,
                policy_ids=[],
                level_codes=[],
                category_codes=query_category_codes,
            ),
            query_ids,
        )
        category_vector = _without_query(
            repository.vector_search(
                query_vector=query_vector,
                profile=embedding_client.profile,
                top_k=100,
                policy_ids=[],
                level_codes=[],
                category_codes=query_category_codes,
            ),
            query_ids,
        )
        category_fusion = _fuse_candidates(
            {
                "keyword": RetrievalBranchResult(category_keyword[:30]),
                "vector": RetrievalBranchResult(category_vector[:30]),
            }
        )
        query_titles = {str(item["policy_title"]) for item in case["query_units"]}
        inferred_category_codes = sorted(
            {infer_coarse_policy_category(title) for title in query_titles}
        )
        inferred_category_policy_ids = sorted(
            {
                policy_id
                for category_code in inferred_category_codes
                for policy_id in policy_ids_by_inferred_category[category_code]
            }
        )
        inferred_category_keyword = _without_query(
            repository.keyword_search(
                query=case["query_text"],
                top_k=100,
                policy_ids=inferred_category_policy_ids,
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        inferred_category_vector = _without_query(
            repository.vector_search(
                query_vector=query_vector,
                profile=embedding_client.profile,
                top_k=100,
                policy_ids=inferred_category_policy_ids,
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        inferred_category_fusion = _fuse_candidates(
            {
                "keyword": RetrievalBranchResult(inferred_category_keyword[:30]),
                "vector": RetrievalBranchResult(inferred_category_vector[:30]),
            }
        )
        policy_group_codes = sorted({infer_policy_category(title) for title in query_titles})
        policy_group_policy_ids = sorted(
            {
                policy_id
                for group_code in policy_group_codes
                for policy_id in policy_ids_by_group[group_code]
            }
        )
        policy_group_keyword = _without_query(
            repository.keyword_search(
                query=case["query_text"],
                top_k=100,
                policy_ids=policy_group_policy_ids,
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        policy_group_vector = _without_query(
            repository.vector_search(
                query_vector=query_vector,
                profile=embedding_client.profile,
                top_k=100,
                policy_ids=policy_group_policy_ids,
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        policy_group_fusion = _fuse_candidates(
            {
                "keyword": RetrievalBranchResult(policy_group_keyword[:30]),
                "vector": RetrievalBranchResult(policy_group_vector[:30]),
            }
        )
        family_policy_ids = sorted(
            {
                policy_id
                for title in query_titles
                for policy_id in policy_ids_by_title[title]
            }
        )
        family_keyword = _without_query(
            repository.keyword_search(
                query=case["query_text"],
                top_k=100,
                policy_ids=family_policy_ids,
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        family_vector = _without_query(
            repository.vector_search(
                query_vector=query_vector,
                profile=embedding_client.profile,
                top_k=100,
                policy_ids=family_policy_ids,
                level_codes=[],
                category_codes=[],
            ),
            query_ids,
        )
        family_rrf = _rrf(family_keyword, family_vector)
        family_pool = _branch_preserving_pool(
            family_rrf,
            family_keyword,
            family_vector,
            limit=40,
            branch_quota=10,
        )
        metadata_augmented_pool = _merge_pools(
            family_pool,
            improved_pool,
            limit=80,
        )
        strategies: dict[str, list[dict[str, Any]]] = {
            "keyword": keyword,
            "vector": vector,
            "current_fusion": current_fusion,
            "improved_rrf": improved_rrf,
            "category_keyword": category_keyword,
            "category_vector": category_vector,
            "category_fusion": category_fusion,
            "inferred_category_keyword": inferred_category_keyword,
            "inferred_category_vector": inferred_category_vector,
            "inferred_category_fusion": inferred_category_fusion,
            "policy_group_keyword": policy_group_keyword,
            "policy_group_vector": policy_group_vector,
            "policy_group_fusion": policy_group_fusion,
            "family_vector": family_vector,
            "family_rrf": family_rrf,
        }
        if current_reranker is not None and current_fusion:
            strategies["current_rerank"] = _rerank(
                current_reranker,
                case["query_text"],
                current_fusion[:30],
                top_n=20,
            )
        if improved_reranker is not None and improved_pool:
            strategies["improved_rerank"] = _rerank(
                improved_reranker,
                case["query_text"],
                improved_pool,
                top_n=20,
            )
        if improved_reranker is not None and metadata_augmented_pool:
            strategies["metadata_augmented_rerank"] = _rerank(
                improved_reranker,
                case["query_text"],
                metadata_augmented_pool,
                top_n=20,
            )

        strategy_results: dict[str, Any] = {}
        for name, items in strategies.items():
            target_group_ranks = [_rank_for_group(items, group) for group in case["target_groups"]]
            finite_ranks = [rank for rank in target_group_ranks if rank is not None]
            strategy_results[name] = {
                "best_rank": min(finite_ranks) if finite_ranks else None,
                "target_group_ranks": target_group_ranks,
                "top_ids": [str(item["id"]) for item in items[:20]],
            }
        case_results.append(
            {
                "sample_id": case["sample_id"],
                "error_type": case["error_type"],
                "assigned_regulation": case["assigned_regulation"],
                "query_unit_ids": sorted(query_ids),
                "query_policy_titles": sorted({str(item["policy_title"]) for item in case["query_units"]}),
                "query_category_codes": query_category_codes,
                "inferred_category_codes": inferred_category_codes,
                "inferred_category_policy_ids": inferred_category_policy_ids,
                "policy_group_codes": policy_group_codes,
                "policy_group_policy_ids": policy_group_policy_ids,
                "family_policy_ids": family_policy_ids,
                "target_unit_ids": [sorted(str(item["id"]) for item in group) for group in case["target_groups"]],
                "target_policy_titles": [
                    sorted({str(item["policy_title"]) for item in group}) for group in case["target_groups"]
                ],
                "target_category_codes": [
                    sorted({str(item["category_code"]) for item in group}) for group in case["target_groups"]
                ],
                "target_inferred_category_codes": [
                    sorted({infer_coarse_policy_category(str(item["policy_title"])) for item in group})
                    for group in case["target_groups"]
                ],
                "target_policy_group_codes": [
                    sorted({infer_policy_category(str(item["policy_title"])) for item in group})
                    for group in case["target_groups"]
                ],
                "candidate_counts": {
                    "keyword": len(keyword),
                    "vector": len(vector),
                    "current_fusion": len(current_fusion),
                    "improved_pool": len(improved_pool),
                    "category_keyword": len(category_keyword),
                    "category_vector": len(category_vector),
                    "category_fusion": len(category_fusion),
                    "inferred_category_keyword": len(inferred_category_keyword),
                    "inferred_category_vector": len(inferred_category_vector),
                    "inferred_category_fusion": len(inferred_category_fusion),
                    "policy_group_keyword": len(policy_group_keyword),
                    "policy_group_vector": len(policy_group_vector),
                    "policy_group_fusion": len(policy_group_fusion),
                    "family_keyword": len(family_keyword),
                    "family_vector": len(family_vector),
                    "family_pool": len(family_pool),
                    "metadata_augmented_pool": len(metadata_augmented_pool),
                },
                "strategies": strategy_results,
            }
        )

    strategy_names = list(case_results[0]["strategies"])
    report = {
        "benchmark": {
            "cases": len(case_results),
            "query_direction": "modified_texts[-1] -> modified_texts[:-1]",
            "corpus_units": len(units),
            "ks": list(KS),
            "reranker_run": run_reranker,
            "sample_scope": "all_conflicts" if all_conflicts else "stratified_24",
            "notes": [
                "case_any_target_recall is lenient for three-level cases; case_all_targets_recall requires every preceding rule.",
                "The query unit itself is removed from candidates because the benchmark simulates checking a new/changed rule.",
                "No atomic assertions are used; this isolates improvements to the existing raw-clause dual retrieval.",
                "Metadata augmentation runs title-family-scoped keyword/vector recall as a parallel path, then unions it with global recall; it is not a hard global filter.",
                "Stored category uses the current database value; inferred category recomputes the same coarse taxonomy from policy title to isolate historical classification errors.",
                "Policy group is a finer title-derived business taxonomy and is evaluated as a scoped parallel retrieval path, not as a global hard filter.",
            ],
        },
        "summary": {name: _summarize(case_results, name) for name in strategy_names},
        "cases": case_results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Proof conflict-oriented dual retrieval.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/conflict-method-eval/retrieval-benchmark.json"),
    )
    parser.add_argument("--skip-reranker", action="store_true")
    parser.add_argument("--all-conflicts", action="store_true")
    args = parser.parse_args()
    report = run(
        args.output,
        run_reranker=not args.skip_reranker,
        all_conflicts=args.all_conflicts,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
