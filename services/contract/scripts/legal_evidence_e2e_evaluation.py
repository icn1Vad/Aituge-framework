from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from contract.config import Settings
from contract.legal_evidence.embedding import OpenAICompatibleLegalEmbeddingProvider
from contract.legal_evidence.models import LegalEvidenceIssue, LegalEvidencePlanRequest
from contract.legal_evidence.planner import (
    PLANNER_VERSION,
    AdaptiveLegalEvidencePlanner,
    LegalEvidencePlannerSafety,
)
from contract.legal_evidence.postgres_repository import (
    PostgresLegalEvidenceRepository,
)
from contract.legal_evidence.reranking import OpenAICompatibleLegalReranker


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    domain: str
    question: str
    required_concepts: tuple[str, ...]
    expected_references: tuple[tuple[str, str], ...]
    expect_zero: bool = False


CASES = (
    EvaluationCase(
        case_id="contract-performance",
        domain="performance_obligations",
        question="合同当事人是否应当按照约定全面履行义务并遵守诚信原则",
        required_concepts=("按照约定", "全面履行", "诚信原则"),
        expected_references=(("中华人民共和国民法典", "第五百零九条"),),
    ),
    EvaluationCase(
        case_id="unilateral-scope-change",
        domain="performance_obligations",
        question="一方能否单方扩大技术服务范围且不调整价款与工期，合同变更应遵守什么规则",
        required_concepts=("协商一致", "变更合同"),
        expected_references=(("中华人民共和国民法典", "第五百四十三条"),),
    ),
    EvaluationCase(
        case_id="liquidated-damages",
        domain="liability_remedies_exit",
        question="约定违约金过分高于实际损失时能否请求人民法院适当减少",
        required_concepts=("违约金", "过分高于", "适当减少"),
        expected_references=(("中华人民共和国民法典", "第五百八十五条"),),
    ),
    EvaluationCase(
        case_id="personal-information-transfer",
        domain="ip_confidentiality_data",
        question="向其他个人信息处理者提供个人信息时是否需要告知接收方并取得单独同意",
        required_concepts=("其他个人信息处理者", "单独同意"),
        expected_references=(("中华人民共和国个人信息保护法", "第二十三条"),),
    ),
    EvaluationCase(
        case_id="lease-repair",
        domain="performance_obligations",
        question="房屋租赁期间出租人的维修义务以及承租人自行维修后的费用承担",
        required_concepts=("维修义务", "维修费用"),
        expected_references=(("中华人民共和国民法典", "第七百一十二条"),),
    ),
    EvaluationCase(
        case_id="construction-permit",
        domain="formation_validity_authority",
        question="建筑工程开工前建设单位是否必须申请领取施工许可证",
        required_concepts=("开工前", "施工许可证"),
        expected_references=(("中华人民共和国建筑法", "第七条"),),
    ),
    EvaluationCase(
        case_id="no-reliable-law",
        domain="missing_ambiguity_completeness",
        question="火星背面量子矿权转让是否必须取得银河委员会蓝色印章",
        required_concepts=("银河委员会", "蓝色印章"),
        expected_references=(),
        expect_zero=True,
    ),
)


def _issue_id(case_id: str) -> str:
    return "legal-issue-" + hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:32]


def _refs(items) -> list[dict[str, str]]:
    return [
        {
            "title": item.unit.title,
            "article_no": item.unit.article_no or "",
            "unit_id": item.unit.unit_id,
        }
        for item in items
    ]


def _reference_set(items) -> set[tuple[str, str]]:
    return {(item.unit.title, item.unit.article_no or "") for item in items}


def _reciprocal_rank(items, expected: set[tuple[str, str]]) -> float:
    for rank, item in enumerate(items, start=1):
        if (item.unit.title, item.unit.article_no or "") in expected:
            return 1.0 / rank
    return 0.0


def _target_only_concept(source_text: str, target_text: str) -> str | None:
    """Return a target phrase that cannot be covered by the source unit."""
    normalized_source = re.sub(r"\s+", "", source_text)
    for run in re.findall(r"[\u4e00-\u9fff]{6,40}", target_text):
        for width in (16, 14, 12, 10, 8, 6):
            if len(run) < width:
                continue
            for offset in range(0, len(run) - width + 1):
                phrase = run[offset : offset + width]
                if phrase not in normalized_source:
                    return phrase
    return None


def _graph_probe(
    repository: PostgresLegalEvidenceRepository,
    release_id: str,
    *,
    jurisdiction: str,
    review_as_of_date: date,
    contract_date: date,
) -> dict[str, Any]:
    """Prove that the planner adds evidence unavailable to initial recall.

    Keyword and vector channels are deliberately isolated in this probe. The
    starting unit still comes from the real exact-search implementation, and
    expansion uses the real verified PostgreSQL graph. This makes the before /
    after set explicit instead of counting a target already present in the
    hybrid top page as graph-added.
    """
    with repository._connect() as conn:  # evaluation-only inspection
        rows = conn.execute(
            """
            SELECT r.relation_id, r.source_unit_id, r.target_unit_id,
                   r.relation_type, r.evidence_text, r.verification_status,
                   s.title AS source_title, s.article_no AS source_article,
                   s.content AS source_content,
                   t.title AS target_title, t.article_no AS target_article,
                   t.content AS target_content
            FROM legal_evidence_relation r
            JOIN legal_evidence_unit s
              ON s.release_id = r.release_id AND s.unit_id = r.source_unit_id
            JOIN legal_evidence_unit t
              ON t.release_id = r.release_id AND t.unit_id = r.target_unit_id
            WHERE r.release_id = %s
              AND r.relation_type <> 'INTERNAL_REF'
              AND r.verification_status IN ('VERIFIED', 'AUTO_VERIFIED')
              AND s.jurisdiction = %s
              AND t.jurisdiction = %s
              AND position('《' in s.title) = 0
              AND char_length(s.title) < 100
              AND s.article_no IS NOT NULL
              AND t.article_no IS NOT NULL
              AND s.content <> t.content
              AND (
                    SELECT count(*)
                    FROM legal_evidence_relation source_edges
                    WHERE source_edges.release_id = r.release_id
                      AND (source_edges.source_unit_id = s.unit_id
                           OR source_edges.target_unit_id = s.unit_id)
                      AND source_edges.verification_status IN (
                            'VERIFIED', 'AUTO_VERIFIED'
                          )
                  ) <= 6
              AND (
                    SELECT count(*)
                    FROM legal_evidence_relation target_edges
                    WHERE target_edges.release_id = r.release_id
                      AND (target_edges.source_unit_id = t.unit_id
                           OR target_edges.target_unit_id = t.unit_id)
                      AND target_edges.verification_status IN (
                            'VERIFIED', 'AUTO_VERIFIED'
                          )
                  ) <= 6
            ORDER BY CASE r.relation_type
                       WHEN 'REPEALS' THEN 1 WHEN 'REPLACES' THEN 2
                       WHEN 'AMENDS' THEN 3 WHEN 'INTERPRETS' THEN 4 ELSE 5
                     END,
                     r.relation_id
            LIMIT 100
            """,
            (release_id, jurisdiction, jurisdiction),
        ).fetchall()
    chosen: tuple[Any, str] | None = None
    for row in rows:
        concept = _target_only_concept(
            " ".join((row["source_title"], row["source_content"])),
            " ".join((row["target_title"], row["target_content"])),
        )
        if concept:
            chosen = (row, concept)
            break
    if chosen is None:
        return {"passed": False, "reason": "NO_VERIFIED_RELATION"}

    row, concept = chosen
    reference = (str(row["source_title"]), str(row["source_article"]))
    initial = repository.exact_search(
        release_id=release_id,
        references=[reference],
        jurisdiction=jurisdiction,
        as_of_date=review_as_of_date.isoformat(),
        limit=24,
    )

    class _ExactAndGraphRepository:
        def active_release(self):
            return repository.active_release()

        def exact_search(self, **kwargs):
            return repository.exact_search(**kwargs)

        @staticmethod
        def keyword_search(**_kwargs):
            return []

        @staticmethod
        def vector_search(**_kwargs):
            return []

        def relation_neighbors(self, **kwargs):
            return repository.relation_neighbors(**kwargs)

    issue_id = _issue_id("verified-graph-expansion-probe")
    request = LegalEvidencePlanRequest(
        review_id="evaluation-verified-graph-expansion-probe",
        generation_id="legal-evidence-e2e-v1",
        contract_type="AUTO",
        jurisdiction=jurisdiction,
        contract_date=contract_date,
        review_as_of_date=review_as_of_date,
        planner_version=PLANNER_VERSION,
        reranker_version="not-used-in-exact-graph-probe",
        issues=[
            LegalEvidenceIssue(
                issue_id=issue_id,
                domain="cross_clause_consistency",
                query=f"请核验《{reference[0]}》{reference[1]}，并确认{concept}",
                question=f"该法规关系能否补充证明{concept}",
                facts=[f"初始证据为《{reference[0]}》{reference[1]}"],
                source_domain="cross_clause_consistency",
                evidence_need=[concept],
                required_concepts=[concept],
            )
        ],
    )
    bundle = AdaptiveLegalEvidencePlanner(
        _ExactAndGraphRepository(),  # type: ignore[arg-type]
        safety=LegalEvidencePlannerSafety(
            candidate_page_size=24,
            maximum_examined_candidates=100,
            maximum_rounds=100,
            maximum_wall_time_seconds=30,
        ),
    ).plan(request)
    initial_ids = {item.unit.unit_id for item in initial}
    added = [
        item
        for item in bundle.evidence
        if item.unit.unit_id not in initial_ids and item.relation_path
    ]
    reached = any(
        item.unit.unit_id == row["target_unit_id"]
        and row["relation_id"] in item.relation_path
        for item in added
    )
    return {
        "passed": reached,
        "relation_id": row["relation_id"],
        "relation_type": row["relation_type"],
        "verification_status": row["verification_status"],
        "evidence_text": row["evidence_text"],
        "source_unit_id": row["source_unit_id"],
        "target_unit_id": row["target_unit_id"],
        "source_title": row["source_title"],
        "source_article": row["source_article"],
        "target_title": row["target_title"],
        "target_article": row["target_article"],
        "required_concept": concept,
        "initial_unit_ids": sorted(initial_ids),
        "graph_added_unit_ids": sorted(item.unit.unit_id for item in added),
        "bundle_status": bundle.status,
        "stop_reason": bundle.stop_reason,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings(database_url=args.postgres_url)
    repository = PostgresLegalEvidenceRepository(settings)
    release = repository.active_release()
    if release is None:
        raise RuntimeError("No active legal evidence projection")
    if args.expected_release_id and release.release_id != args.expected_release_id:
        raise RuntimeError(
            f"Active projection is {release.release_id}, expected {args.expected_release_id}"
        )
    embedding = OpenAICompatibleLegalEmbeddingProvider(
        base_url=args.model_gateway_url,
        registration_id=args.embedding_registration_id,
        model=args.embedding_model,
        api_key=args.api_key,
    )
    reranker = OpenAICompatibleLegalReranker(
        base_url=args.model_gateway_url,
        registration_id=args.reranker_registration_id,
        model=args.reranker_model,
        api_key=args.api_key,
        timeout_seconds=args.model_timeout_seconds,
    )
    planner = AdaptiveLegalEvidencePlanner(
        repository,
        embedding_provider=embedding,
        reranker=reranker,
        safety=LegalEvidencePlannerSafety(
            maximum_wall_time_seconds=args.issue_timeout_seconds,
            maximum_total_wall_time_seconds=args.total_timeout_seconds,
            maximum_examined_candidates=160,
            maximum_total_examined_candidates=1120,
        ),
    )
    case_results: list[dict[str, Any]] = []
    for case in CASES:
        started = time.monotonic()
        vector = embedding.embed_query(case.question)
        exact = repository.exact_search(
            release_id=release.release_id,
            references=list(case.expected_references),
            jurisdiction=args.jurisdiction,
            as_of_date=args.review_as_of_date.isoformat(),
            limit=24,
        )
        keyword = repository.keyword_search(
            release_id=release.release_id,
            query=case.question,
            jurisdiction=args.jurisdiction,
            as_of_date=args.review_as_of_date.isoformat(),
            offset=0,
            limit=24,
        )
        vector_hits = repository.vector_search(
            release_id=release.release_id,
            query_vector=vector,
            embedding_profile_id=embedding.profile_id,
            jurisdiction=args.jurisdiction,
            as_of_date=args.review_as_of_date.isoformat(),
            offset=0,
            limit=24,
        )
        request = LegalEvidencePlanRequest(
            review_id=f"evaluation-{case.case_id}",
            generation_id="legal-evidence-e2e-v1",
            contract_type="AUTO",
            jurisdiction=args.jurisdiction,
            contract_date=args.contract_date,
            review_as_of_date=args.review_as_of_date,
            planner_version=PLANNER_VERSION,
            reranker_version=(
                f"{args.reranker_registration_id}:{args.reranker_model}"
            ),
            issues=[
                LegalEvidenceIssue(
                    issue_id=_issue_id(case.case_id),
                    domain=case.domain,
                    query=case.question,
                    question=case.question,
                    facts=[case.question],
                    source_domain=case.domain,
                    evidence_need=list(case.required_concepts),
                    required_concepts=list(case.required_concepts),
                )
            ],
        )
        bundle = planner.plan(request)
        expected = {(title, article or "") for title, article in case.expected_references}
        exact_refs = _reference_set(exact)
        keyword_refs = _reference_set(keyword)
        vector_refs = _reference_set(vector_hits)
        bundle_refs = {
            (item.unit.title, item.unit.article_no or "") for item in bundle.evidence
        }
        initial_unit_ids = {
            item.unit.unit_id for item in (*exact, *keyword, *vector_hits)
        }
        graph_added = [
            item
            for item in bundle.evidence
            if "RELATION" in item.retrieval_channels
            and item.unit.unit_id not in initial_unit_ids
        ]
        expected_hit = bool(expected & bundle_refs) if expected else not bundle.evidence
        traceable = all(
            item.unit.release_id == release.release_id
            and bool(item.unit.version_id)
            and bool(item.unit.source_node_ids)
            and bool(item.unit.content)
            and all(
                relation_id in {relation.relation_id for relation in bundle.relations}
                for relation_id in item.relation_path
            )
            for item in bundle.evidence
        )
        case_results.append(
            {
                "case": asdict(case),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "exact": _refs(exact),
                "keyword": _refs(keyword),
                "vector": _refs(vector_hits),
                "bundle_status": bundle.status,
                "stop_reason": bundle.stop_reason,
                "bundle_references": [
                    {
                        "title": item.unit.title,
                        "article_no": item.unit.article_no,
                        "unit_id": item.unit.unit_id,
                        "channels": item.retrieval_channels,
                        "score": item.relevance_score,
                        "rerank_score": item.rerank_score,
                        "source_node_ids": item.unit.source_node_ids,
                    }
                    for item in bundle.evidence
                ],
                "relation_paths": [
                    item.model_dump(mode="json") for item in bundle.relation_paths
                ],
                "applicability_decisions": [
                    item.model_dump(mode="json")
                    for item in bundle.applicability_decisions
                ],
                "expected_hit": expected_hit,
                "exact_expected_hit": bool(expected & exact_refs),
                "keyword_expected_hit": bool(expected & keyword_refs),
                "vector_expected_hit": bool(expected & vector_refs),
                "hybrid_reciprocal_rank": _reciprocal_rank(
                    bundle.evidence, expected
                ),
                "keyword_reciprocal_rank": _reciprocal_rank(keyword, expected),
                "vector_reciprocal_rank": _reciprocal_rank(vector_hits, expected),
                "traceable": traceable,
                "graph_added_evidence_count": len(graph_added),
                "evidence_count": len(bundle.evidence),
                "duplicate_evidence_count": len(bundle.evidence) - len(bundle_refs),
                "unresolved_issue_ids": bundle.unresolved_issue_ids,
                "conflicts": [item.model_dump(mode="json") for item in bundle.conflicts],
                "degraded_channels": bundle.degraded_channels,
            }
        )
    positive = [item for item in case_results if not item["case"]["expect_zero"]]
    expected_hits = sum(int(item["expected_hit"]) for item in positive)
    all_evidence = sum(item["evidence_count"] for item in case_results)
    duplicate_count = sum(item["duplicate_evidence_count"] for item in case_results)
    graph_added_count = sum(item["graph_added_evidence_count"] for item in case_results)
    hybrid_mrr = sum(item["hybrid_reciprocal_rank"] for item in positive) / max(
        1, len(positive)
    )
    keyword_mrr = sum(item["keyword_reciprocal_rank"] for item in positive) / max(
        1, len(positive)
    )
    vector_mrr = sum(item["vector_reciprocal_rank"] for item in positive) / max(
        1, len(positive)
    )
    graph_probe = _graph_probe(
        repository,
        release.release_id,
        jurisdiction=args.jurisdiction,
        review_as_of_date=args.review_as_of_date,
        contract_date=args.contract_date,
    )
    probe_added_count = len(graph_probe.get("graph_added_unit_ids", ()))
    return {
        "schema_version": "1.0",
        "generated_at_epoch": int(time.time()),
        "projection": release.model_dump(mode="json"),
        "review_as_of_date": args.review_as_of_date.isoformat(),
        "contract_date": args.contract_date.isoformat(),
        "planner_version": PLANNER_VERSION,
        "embedding_model_version": f"{args.embedding_registration_id}:{args.embedding_model}",
        "reranker_version": f"{args.reranker_registration_id}:{args.reranker_model}",
        "metrics": {
            "applicable_issue_coverage": expected_hits / max(1, len(positive)),
            "zero_evidence_correct_rejection": all(
                item["expected_hit"]
                for item in case_results
                if item["case"]["expect_zero"]
            ),
            "traceability_rate": sum(
                int(item["traceable"]) for item in case_results
            )
            / max(1, len(case_results)),
            "duplicate_evidence_ratio": duplicate_count / max(1, all_evidence),
            "graph_expansion_added_evidence_ratio": (
                graph_added_count + probe_added_count
            )
            / max(1, all_evidence + probe_added_count),
            "hybrid_case_graph_expansion_added_evidence_ratio": graph_added_count
            / max(1, all_evidence),
            "verified_graph_expansion_probe_passed": bool(graph_probe.get("passed")),
            "hybrid_expected_hits": expected_hits,
            "keyword_expected_hits": sum(
                int(item["keyword_expected_hit"]) for item in positive
            ),
            "vector_expected_hits": sum(
                int(item["vector_expected_hit"]) for item in positive
            ),
            "exact_expected_hits": sum(
                int(item["exact_expected_hit"]) for item in positive
            ),
            "hybrid_mrr": hybrid_mrr,
            "keyword_mrr": keyword_mrr,
            "vector_mrr": vector_mrr,
            "hybrid_mrr_beats_each_non_oracle_single_channel": hybrid_mrr
            > max(keyword_mrr, vector_mrr),
            "elapsed_seconds": round(
                sum(item["elapsed_seconds"] for item in case_results), 3
            ),
        },
        "graph_probe": graph_probe,
        "cases": case_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real PostgreSQL legal-evidence E2E evaluation"
    )
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--model-gateway-url", required=True)
    parser.add_argument(
        "--api-key", default=os.getenv("LEGAL_EVALUATION_API_KEY", "")
    )
    parser.add_argument("--embedding-registration-id", default="policy-embedding-v4")
    parser.add_argument("--embedding-model", default="text-embedding-v4")
    parser.add_argument("--reranker-registration-id", default="api-qwen3-rerank")
    parser.add_argument("--reranker-model", default="qwen3-rerank")
    parser.add_argument("--expected-release-id", default="")
    parser.add_argument("--jurisdiction", default="CN")
    parser.add_argument("--review-as-of-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--contract-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--issue-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--model-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = evaluate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["metrics"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
