from __future__ import annotations

from datetime import date

from contract.application.idempotency import canonical_json
from contract.legal_evidence.binding import LegalEvidenceCheckBinder
from contract.legal_evidence.models import (
    LegalEvidence,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidenceRelease,
    LegalRelation,
    LegalRetrievalUnit,
    LegalSearchCandidate,
)
from contract.legal_evidence.planner import AdaptiveLegalEvidencePlanner
from contract.legal_evidence.prompting import (
    compact_legal_evidence_catalog,
    deterministic_token_upper_bound,
    legal_evidence_ids_for_check,
    remaining_legal_prompt_budget,
)
from contract.legal_evidence.testing import (
    InMemoryLegalEvidenceRepository,
    StaticLegalEmbeddingProvider,
    StaticLegalReranker,
)


def _unit(
    unit_id: str,
    content: str,
    *,
    instrument_id: str = "instrument-1",
    article_no: str = "第一条",
    jurisdiction: str | None = "CN",
) -> LegalRetrievalUnit:
    return LegalRetrievalUnit(
        unit_id=unit_id,
        release_id="release-1",
        instrument_id=instrument_id,
        version_id=f"version-{instrument_id}",
        source_node_ids=[f"node-{unit_id}"],
        title="测试法规",
        article_no=article_no,
        heading_path=[article_no],
        content=content,
        jurisdiction=jurisdiction,
        authority_level=None,
        issuing_authority=None,
        effective_from=None,
        effective_to=None,
        validity_status=None,
        metadata_verification_status="UNVERIFIED",
        official_source_url=None,
        content_hash="a" * 64,
        sequence=1,
    )


def _request(*concepts: str) -> LegalEvidencePlanRequest:
    return LegalEvidencePlanRequest(
        review_id="review-1",
        generation_id="generation-1",
        contract_type="AUTO",
        jurisdiction="CN",
        contract_date=None,
        review_as_of_date=date(2026, 8, 31),
        issues=[
            LegalEvidenceIssue(
                issue_id="legal-issue-" + "1" * 32,
                domain="liability_remedies_exit",
                query="违约金规则与解除合同的法律后果",
                required_concepts=list(concepts),
            )
        ],
    )


def test_no_active_release_returns_empty_bundle_for_strict_legacy_fallback() -> None:
    planner = AdaptiveLegalEvidencePlanner(InMemoryLegalEvidenceRepository())

    bundle = planner.plan(_request("违约金"))

    assert bundle.status == "NO_ACTIVE_RELEASE"
    assert bundle.evidence == []
    assert bundle.release_id is None
    assert bundle.stop_reason == "NO_ACTIVE_RELEASE"


def test_keyword_and_vector_are_fused_and_vector_failure_degrades() -> None:
    first = _unit("unit-1", "当事人可以约定违约金。")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
            embedding_profile_id="test-profile",
        ),
        keyword_pages=[[LegalSearchCandidate(unit=first, score=0.8, channel="KEYWORD")]],
        vector_error=RuntimeError("embedding unavailable"),
    )
    planner = AdaptiveLegalEvidencePlanner(
        repository,
        embedding_provider=StaticLegalEmbeddingProvider([0.1, 0.2]),
    )

    bundle = planner.plan(_request("违约金"))

    assert bundle.status == "DEGRADED"
    assert [item.unit.unit_id for item in bundle.evidence] == ["unit-1"]
    assert bundle.coverage[0].complete is True
    assert bundle.degraded_channels == ["VECTOR"]
    assert bundle.stop_reason == "COVERAGE_SATISFIED"


def test_same_unit_from_keyword_and_vector_preserves_both_channels() -> None:
    first = _unit("unit-1", "当事人可以约定违约金。")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
            embedding_profile_id="test-profile",
        ),
        keyword_pages=[
            [LegalSearchCandidate(unit=first, score=0.8, channel="KEYWORD")]
        ],
        vector_pages=[
            [LegalSearchCandidate(unit=first, score=0.9, channel="VECTOR")]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(
        repository,
        embedding_provider=StaticLegalEmbeddingProvider([0.1, 0.2]),
    ).plan(_request("违约金"))

    assert bundle.evidence[0].retrieval_channels == ["KEYWORD", "VECTOR"]


def test_uncorroborated_vector_only_hit_is_not_promoted_to_legal_evidence() -> None:
    unrelated = _unit("unit-vector-only", "一般行政管理程序。")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
            embedding_profile_id="test-profile",
        ),
        vector_pages=[
            [LegalSearchCandidate(unit=unrelated, score=0.82, channel="VECTOR")]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(
        repository,
        embedding_provider=StaticLegalEmbeddingProvider([0.1, 0.2]),
    ).plan(_request("火星采矿许可"))

    assert bundle.status == "NO_RELEVANT_EVIDENCE"
    assert bundle.evidence == []
    assert bundle.unresolved_issue_ids == ["legal-issue-" + "1" * 32]


def test_vector_only_hit_can_be_promoted_by_a_calibrated_reranker() -> None:
    paraphrased = _unit("unit-vector-reranked", "约定损害赔偿数额可以调整。")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
            embedding_profile_id="test-profile",
        ),
        vector_pages=[
            [LegalSearchCandidate(unit=paraphrased, score=0.82, channel="VECTOR")]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(
        repository,
        embedding_provider=StaticLegalEmbeddingProvider([0.1, 0.2]),
        reranker=StaticLegalReranker({paraphrased.unit_id: 0.91}),
    ).plan(_request("违约金"))

    assert [item.unit.unit_id for item in bundle.evidence] == [
        paraphrased.unit_id
    ]
    assert bundle.evidence[0].rerank_score == 0.91


def test_low_reranker_score_rejects_uncorroborated_keyword_hit() -> None:
    unrelated = _unit("unit-keyword-reranked", "矿权转让应当依法办理登记。")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[
            [LegalSearchCandidate(unit=unrelated, score=0.82, channel="KEYWORD")]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(
        repository,
        reranker=StaticLegalReranker({unrelated.unit_id: 0.45}),
    ).plan(_request("银河委员会", "蓝色印章"))

    assert bundle.status == "NO_RELEVANT_EVIDENCE"
    assert bundle.evidence == []
    assert bundle.unresolved_issue_ids == ["legal-issue-" + "1" * 32]


def test_explicit_instrument_article_uses_exact_channel_and_freezes_versions() -> None:
    unit = _unit("unit-exact", "当事人应当按照约定全面履行自己的义务。")
    unit = unit.model_copy(update={"title": "中华人民共和国民法典", "article_no": "第五百零九条"})
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="projection-1",
            source_release_id="legal-release-1",
            status="ACTIVE",
            projection_version="projection-v4",
            relation_extractor_version="extractor-v2",
            embedding_model_version="embedding-v4",
        ),
        exact_results=[LegalSearchCandidate(unit=unit, score=1, channel="EXACT")],
    )
    request = _request("履行").model_copy(
        update={
            "reranker_version": "reranker-v1",
            "issues": [
                _request("履行").issues[0].model_copy(
                    update={"query": "请核验《中华人民共和国民法典》第五百零九条"}
                )
            ],
        }
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(request)

    assert bundle.evidence[0].retrieval_channels == ["EXACT"]
    assert bundle.version_snapshot is not None
    assert bundle.version_snapshot.legal_release_id == "legal-release-1"
    assert bundle.version_snapshot.legal_projection_version == "projection-v4"
    assert bundle.version_snapshot.relation_extractor_version == "extractor-v2"
    assert bundle.version_snapshot.embedding_model_version == "embedding-v4"
    assert bundle.version_snapshot.reranker_version == "reranker-v1"


def test_law_effective_after_contract_date_is_not_formation_basis() -> None:
    unit = _unit("unit-later", "合同依法成立并生效。")
    unit = unit.model_copy(
        update={
            "effective_from": date(2026, 1, 1),
            "validity_status": "ACTIVE",
            "metadata_verification_status": "VERIFIED",
        }
    )
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="projection-v4",
        ),
        keyword_pages=[[LegalSearchCandidate(unit=unit, score=1, channel="KEYWORD")]],
    )
    request = _request("生效").model_copy(
        update={
            "contract_date": date(2025, 1, 1),
            "issues": [
                _request("生效").issues[0].model_copy(
                    update={"domain": "formation_validity_authority"}
                )
            ],
        }
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(request)

    assert bundle.evidence == []
    assert bundle.status == "NO_RELEVANT_EVIDENCE"


def test_reranker_score_is_used_and_persisted_in_bundle() -> None:
    first = _unit("unit-1", "违约金规则")
    second = _unit("unit-2", "解除合同规则", article_no="第二条")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[
            LegalSearchCandidate(unit=first, score=0.9, channel="KEYWORD"),
            LegalSearchCandidate(unit=second, score=0.8, channel="KEYWORD"),
        ]],
    )
    planner = AdaptiveLegalEvidencePlanner(
        repository,
        reranker=StaticLegalReranker({"unit-1": 0.2, "unit-2": 0.95}),
    )

    bundle = planner.plan(_request("违约金", "解除合同"))

    assert bundle.rerank_applied is True
    assert bundle.evidence[0].unit.unit_id == "unit-2"
    assert bundle.evidence[0].rerank_score == 0.95
    assert bundle.rerank_diagnostics == ["RERANK_OK:2"]


def test_reranker_failure_degrades_without_losing_keyword_results() -> None:
    first = _unit("unit-1", "违约金规则")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[
            [LegalSearchCandidate(unit=first, score=0.9, channel="KEYWORD")]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(
        repository,
        reranker=StaticLegalReranker({}, error=RuntimeError("offline")),
    ).plan(_request("违约金"))

    assert [item.unit.unit_id for item in bundle.evidence] == ["unit-1"]
    assert bundle.status == "DEGRADED"
    assert bundle.degraded_channels == ["RERANK"]
    assert bundle.rerank_diagnostics == ["RERANK_DEGRADED:RuntimeError"]


def test_relation_expansion_adds_verified_exception_until_all_concepts_are_covered() -> None:
    base = _unit("unit-1", "当事人可以约定违约金。")
    exception = _unit(
        "unit-2",
        "约定违约金过分高于损失的，人民法院可以适当减少。",
        article_no="第二条",
    )
    relation = LegalRelation(
        relation_id="relation-1",
        release_id="release-1",
        source_unit_id="unit-1",
        target_unit_id="unit-2",
        relation_type="EXCEPTION_TO",
        evidence_text="违约金过分高于损失的可以减少",
        confidence=1,
        verification_status="VERIFIED",
    )
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[LegalSearchCandidate(unit=base, score=0.9, channel="KEYWORD")]],
        relations={"unit-1": [(relation, exception)]},
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(
        _request("违约金", "减少")
    )

    assert [item.unit.unit_id for item in bundle.evidence] == ["unit-1", "unit-2"]
    assert bundle.coverage[0].covered_concepts == ["违约金", "减少"]
    assert bundle.coverage[0].complete is True
    assert [item.relation_id for item in bundle.relations] == ["relation-1"]
    assert bundle.evidence[1].relation_path == ["relation-1"]
    assert bundle.status == "DEGRADED"
    assert bundle.conflicts[0].conflict_type == "RELATION"
    assert bundle.stop_reason != "COVERAGE_SATISFIED"


def test_graph_expansion_stops_after_ordinary_edge_adds_no_new_concept() -> None:
    base = _unit("unit-base", "违约金可以由当事人约定。")
    target = _unit(
        "unit-target",
        "违约金过分高于损失的，人民法院可以适当减少。",
        article_no="第二条",
    )
    irrelevant = _unit(
        "unit-irrelevant",
        "一般行政机关按照年度计划开展统计。",
        article_no="第三条",
    )
    first_relation = LegalRelation(
        relation_id="relation-cites-target",
        release_id="release-1",
        source_unit_id=base.unit_id,
        target_unit_id=target.unit_id,
        relation_type="CITES",
        evidence_text="依照第二条",
        confidence=1,
        verification_status="AUTO_VERIFIED",
    )
    second_relation = LegalRelation(
        relation_id="relation-cites-irrelevant",
        release_id="release-1",
        source_unit_id=target.unit_id,
        target_unit_id=irrelevant.unit_id,
        relation_type="CITES",
        evidence_text="依照第三条",
        confidence=1,
        verification_status="AUTO_VERIFIED",
    )
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[LegalSearchCandidate(unit=base, score=1, channel="KEYWORD")]],
        relations={
            base.unit_id: [(first_relation, target)],
            target.unit_id: [(second_relation, irrelevant)],
        },
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(
        _request("违约金", "减少")
    )

    assert [item.unit.unit_id for item in bundle.evidence] == [
        base.unit_id,
        target.unit_id,
    ]
    assert bundle.coverage[0].complete is True
    assert bundle.stop_reason == "COVERAGE_SATISFIED"


def test_amendment_relation_creates_unresolved_validity_conflict() -> None:
    old_rule = _unit("unit-old", "旧规则适用于付款。")
    amendment = _unit(
        "unit-amendment",
        "新规则修改旧规则。",
        instrument_id="instrument-amendment",
        article_no="第二条",
    )
    relation = LegalRelation(
        relation_id="relation-amends",
        release_id="release-1",
        source_unit_id="unit-amendment",
        target_unit_id="unit-old",
        relation_type="AMENDS",
        evidence_text="修改旧规则",
        confidence=1,
        verification_status="AUTO_VERIFIED",
    )
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[
            [LegalSearchCandidate(unit=old_rule, score=1, channel="KEYWORD")]
        ],
        # This is an inbound edge for unit-old. The repository contract returns
        # both directions while preserving the original relation orientation.
        relations={"unit-old": [(relation, amendment)]},
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(
        _request("旧规则", "新规则")
    )

    assert bundle.conflicts[0].conflict_type == "VALIDITY"
    assert bundle.stop_reason != "COVERAGE_SATISFIED"


def test_verified_metadata_disagreement_creates_applicability_conflict() -> None:
    active = _unit("unit-active", "规则甲", article_no="第一条").model_copy(
        update={
            "validity_status": "ACTIVE",
            "metadata_verification_status": "VERIFIED",
        }
    )
    second_active = _unit("unit-active-v2", "规则乙", article_no="第二条").model_copy(
        update={
            "version_id": "version-instrument-1-v2",
            "validity_status": "ACTIVE",
            "metadata_verification_status": "VERIFIED",
        }
    )
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[
            LegalSearchCandidate(unit=active, score=1, channel="KEYWORD"),
            LegalSearchCandidate(unit=second_active, score=0.9, channel="KEYWORD"),
        ]],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(_request("规则甲", "规则乙"))

    assert bundle.conflicts[0].conflict_type == "APPLICABILITY"
    assert bundle.stop_reason != "COVERAGE_SATISFIED"


def test_candidate_count_is_coverage_driven_not_a_fixed_result_top_k() -> None:
    units = [
        _unit(
            f"unit-{index}",
            f"验收标准覆盖概念{index}。",
            article_no=f"第{index}条",
        )
        for index in range(1, 13)
    ]
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[
            [
                LegalSearchCandidate(
                    unit=unit,
                    score=1 - (index * 0.01),
                    channel="KEYWORD",
                )
                for index, unit in enumerate(units)
            ]
        ],
    )

    request = _request(*(f"概念{index}" for index in range(1, 13)))
    request = request.model_copy(
        update={
            "issues": [
                request.issues[0].model_copy(
                    update={
                        "domain": "commercial_financial",
                        "check_codes": ["CF-008"],
                    }
                )
            ]
        }
    )
    bundle = LegalEvidenceCheckBinder().bind(
        AdaptiveLegalEvidencePlanner(repository).plan(request)
    )

    assert len(bundle.evidence) == 12
    assert bundle.coverage[0].complete is True
    assert bundle.stop_reason == "COVERAGE_SATISFIED"
    catalog, estimated_tokens = compact_legal_evidence_catalog(bundle.evidence)
    assert {item["evidence_id"] for item in catalog} == {
        item.evidence_id for item in bundle.evidence
    }
    assert estimated_tokens > 0


def test_finding_citation_binding_keeps_only_applicable_evidence_ids() -> None:
    first = _unit("unit-1", "价款总额应当按照约定的计价标准确定。")
    second = _unit("unit-2", "当事人应当及时付款。", article_no="第二条")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[
            LegalSearchCandidate(unit=first, score=1, channel="KEYWORD"),
            LegalSearchCandidate(unit=second, score=0.9, channel="KEYWORD"),
        ]],
    )
    request = _request()
    request = request.model_copy(
        update={
            "issues": [
                request.issues[0].model_copy(
                    update={
                        "domain": "commercial_financial",
                        "check_codes": ["CF-001", "CF-002"],
                    }
                )
            ]
        }
    )
    evidence = LegalEvidenceCheckBinder().bind(
        AdaptiveLegalEvidencePlanner(repository).plan(request)
    ).evidence
    unrelated = evidence[0].model_copy(
        update={
            "evidence_id": "legal-evidence-" + "f" * 32,
            "check_codes": ["CF-002"],
        }
    )

    assert legal_evidence_ids_for_check([*evidence, unrelated], "CF-001") == sorted(
        item.evidence_id for item in evidence if item.check_codes == ["CF-001"]
    )


def test_catalog_uses_conservative_budget_and_never_drops_only_some_ids() -> None:
    evidence = [
        LegalEvidence(
            evidence_id="legal-evidence-" + f"{index:032x}",
            issue_ids=["legal-issue-" + "1" * 32],
            check_codes=["CF-001"],
            unit=_unit(f"unit-{index}", "付款" * 300),
            relevance_score=1,
            retrieval_channels=["KEYWORD"],
        )
        for index in range(1, 4)
    ]
    catalog, token_upper_bound = compact_legal_evidence_catalog(
        evidence,
        maximum_catalog_tokens=1800,
    )
    assert {item["evidence_id"] for item in catalog} == {
        item.evidence_id for item in evidence
    }
    assert token_upper_bound <= 1800
    assert token_upper_bound == deterministic_token_upper_bound(canonical_json(catalog))

    omitted, omitted_tokens = compact_legal_evidence_catalog(
        evidence,
        maximum_catalog_tokens=20,
    )
    assert omitted == []
    assert omitted_tokens == 0

    zero_excerpt_catalog = [
        {**item, "content_excerpt": "", "content_truncated": True}
        for item in catalog
    ]
    too_small_for_complete_catalog = deterministic_token_upper_bound(
        canonical_json(zero_excerpt_catalog)
    ) - 1
    omitted, omitted_tokens = compact_legal_evidence_catalog(
        evidence,
        maximum_catalog_tokens=too_small_for_complete_catalog,
    )
    assert omitted == []
    assert omitted_tokens == 0


def test_remaining_legal_budget_reserves_the_legacy_prompt_and_system_prompt() -> None:
    remaining = remaining_legal_prompt_budget(
        baseline_prompt="旧提示词",
        system_prompt="系统提示词",
        hard_limit_tokens=1000,
        safety_tokens=100,
    )
    assert remaining == (
        1000
        - 100
        - deterministic_token_upper_bound("旧提示词")
        - deterministic_token_upper_bound("系统提示词")
    )


def test_candidate_with_wrong_jurisdiction_or_verified_inactive_date_is_not_used() -> None:
    wrong_place = _unit("unit-place", "违约金", jurisdiction="US")
    expired = _unit("unit-expired", "违约金").model_copy(
        update={
            "effective_to": date(2020, 1, 1),
            "validity_status": "EXPIRED",
            "metadata_verification_status": "VERIFIED",
        }
    )
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[
            LegalSearchCandidate(unit=wrong_place, score=1, channel="KEYWORD"),
            LegalSearchCandidate(unit=expired, score=0.9, channel="KEYWORD"),
        ]],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(_request("违约金"))

    assert bundle.status == "NO_RELEVANT_EVIDENCE"
    assert bundle.evidence == []


def test_national_request_does_not_accept_local_regulation() -> None:
    national = _unit("unit-national", "违约金", jurisdiction="CN")
    local = _unit("unit-local", "违约金", jurisdiction="CN-36")
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[
            LegalSearchCandidate(unit=local, score=1, channel="KEYWORD"),
            LegalSearchCandidate(unit=national, score=0.9, channel="KEYWORD"),
        ]],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(_request("违约金"))

    assert [item.unit.unit_id for item in bundle.evidence] == ["unit-national"]


def test_local_request_accepts_its_national_parent_but_not_sibling_locality() -> None:
    national = _unit("unit-national", "一般规则", jurisdiction="CN")
    own_local = _unit("unit-own", "本地规则", jurisdiction="CN-36")
    sibling = _unit("unit-sibling", "其他地区规则", jurisdiction="CN-11")
    request = _request("一般规则", "本地规则")
    request = request.model_copy(update={"jurisdiction": "CN-36"})
    repository = InMemoryLegalEvidenceRepository(
        release=LegalEvidenceRelease(
            release_id="release-1",
            source_release_id="mysql-release-1",
            status="ACTIVE",
            projection_version="legal-projection-v1",
        ),
        keyword_pages=[[
            LegalSearchCandidate(unit=sibling, score=1, channel="KEYWORD"),
            LegalSearchCandidate(unit=national, score=0.9, channel="KEYWORD"),
            LegalSearchCandidate(unit=own_local, score=0.8, channel="KEYWORD"),
        ]],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(request)

    assert {item.unit.unit_id for item in bundle.evidence} == {
        "unit-national",
        "unit-own",
    }
