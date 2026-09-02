from __future__ import annotations

from datetime import date

from contract.legal_evidence.binding import (
    LEGAL_EVIDENCE_CHECK_BINDING_VERSION,
    LegalEvidenceCheckBinder,
)
from contract.legal_evidence.models import (
    LegalEvidenceBundle,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidencePlanSnapshot,
    LegalEvidenceRelease,
    LegalRelation,
    LegalRetrievalUnit,
    LegalSearchCandidate,
)
from contract.legal_evidence.planner import (
    AdaptiveLegalEvidencePlanner,
    LegalEvidencePlannerSafety,
)
from contract.legal_evidence.projection import (
    extract_internal_references,
    extract_named_instrument_references,
)
from contract.legal_evidence.provider import PlannerLegalEvidenceProvider
from contract.legal_evidence.testing import (
    InMemoryLegalEvidenceRepository,
    StaticLegalReranker,
)
from contract.persistence.postgres.migrate import MIGRATIONS_DIR
from risk_test_data import risk_plan_input


def _unit(
    unit_id: str,
    content: str,
    *,
    article_no: str = "第一条",
) -> LegalRetrievalUnit:
    return LegalRetrievalUnit(
        unit_id=unit_id,
        release_id="release-1",
        instrument_id="instrument-1",
        version_id="version-1",
        source_node_ids=[f"node-{unit_id}"],
        title="测试法规",
        article_no=article_no,
        heading_path=[article_no],
        content=content,
        jurisdiction="CN",
        metadata_verification_status="UNVERIFIED",
        content_hash="a" * 64,
        sequence=1,
    )


def _release(*, embedding_profile_id: str | None = None) -> LegalEvidenceRelease:
    return LegalEvidenceRelease(
        release_id="release-1",
        source_release_id="source-release-1",
        status="ACTIVE",
        projection_version="legal-projection-v1",
        embedding_profile_id=embedding_profile_id,
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
                query="违约责任与合同解除",
                check_codes=["LRE-001"],
                required_concepts=list(concepts),
            )
        ],
    )


def _candidate(unit: LegalRetrievalUnit, score: float) -> LegalSearchCandidate:
    return LegalSearchCandidate(unit=unit, score=score, channel="KEYWORD")


def test_safety_budget_can_never_produce_ready_or_usable_bundle() -> None:
    repository = InMemoryLegalEvidenceRepository(
        release=_release(),
        keyword_pages=[
            [
                _candidate(_unit("unit-1", "一般违约规则"), 1.0),
                _candidate(_unit("unit-2", "其他规则", article_no="第二条"), 0.9),
            ]
        ],
    )
    planner = AdaptiveLegalEvidencePlanner(
        repository,
        safety=LegalEvidencePlannerSafety(maximum_examined_candidates=1),
    )

    bundle = planner.plan(_request("不存在的必需概念"))

    assert bundle.stop_reason == "SAFETY_BUDGET_REACHED"
    assert bundle.unresolved_issue_ids
    assert bundle.status != "READY"
    assert bundle.usable is False


def test_keyword_query_uses_legal_concepts_instead_of_full_prompt() -> None:
    issue = LegalEvidenceIssue(
        issue_id="legal-issue-" + "9" * 32,
        domain="commercial_financial",
        query="合同事实：" + "通用背景" * 1500,
        question="价款调整和验收条件应适用哪些规则",
        contract_object="技术服务",
        evidence_need=["价款调整", "验收条件", "合同"],
        required_concepts=["价款调整", "验收条件"],
    )

    query = AdaptiveLegalEvidencePlanner._keyword_query(issue)

    assert query == "价款调整 验收条件 技术服务"
    assert "通用背景" not in query


def test_low_relevance_full_page_stops_as_zero_evidence_not_safety_failure() -> None:
    candidates = [
        _candidate(
            _unit(f"unit-{index}", f"一般行政说明{index}", article_no=f"第{index}条"),
            1 - index * 0.001,
        )
        for index in range(24)
    ]
    reranker = StaticLegalReranker(
        {candidate.unit.unit_id: 0.60 - index * 0.001 for index, candidate in enumerate(candidates)}
    )

    bundle = AdaptiveLegalEvidencePlanner(
        InMemoryLegalEvidenceRepository(
            release=_release(),
            keyword_pages=[candidates],
        ),
        reranker=reranker,
    ).plan(_request("候选证据中没有的概念"))

    assert bundle.status == "NO_RELEVANT_EVIDENCE"
    assert bundle.evidence == []
    assert bundle.stop_reason == "CANDIDATES_EXHAUSTED"


def test_unresolved_issue_can_never_produce_ready_or_usable_bundle() -> None:
    repository = InMemoryLegalEvidenceRepository(
        release=_release(),
        keyword_pages=[[_candidate(_unit("unit-1", "一般违约规则"), 1.0)]],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(
        _request("候选证据中没有的概念")
    )

    assert bundle.stop_reason == "CANDIDATES_EXHAUSTED"
    assert bundle.unresolved_issue_ids == ["legal-issue-" + "1" * 32]
    assert bundle.status != "READY"
    assert bundle.usable is False


def test_relation_conflict_can_never_produce_ready_or_usable_bundle() -> None:
    general = _unit("unit-general", "当事人可以约定违约金。")
    exception = _unit(
        "unit-exception",
        "违约金过分高于损失的，人民法院可以减少。",
        article_no="第二条",
    )
    relation = LegalRelation(
        relation_id="relation-exception",
        release_id="release-1",
        source_unit_id=general.unit_id,
        target_unit_id=exception.unit_id,
        relation_type="EXCEPTION_TO",
        evidence_text="违约金过分高于损失的可以减少",
        confidence=1,
        verification_status="VERIFIED",
    )
    repository = InMemoryLegalEvidenceRepository(
        release=_release(),
        keyword_pages=[[_candidate(general, 1.0)]],
        relations={general.unit_id: [(relation, exception)]},
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(
        _request("违约金", "减少")
    )

    assert bundle.conflicts
    assert bundle.status != "READY"
    assert bundle.stop_reason != "COVERAGE_SATISFIED"
    assert bundle.usable is False


def test_no_concept_dynamic_stop_can_return_many_or_few_not_fixed_three() -> None:
    units = [
        _unit(f"unit-{index}", f"法律问题证据{index}", article_no=f"第{index}条")
        for index in range(1, 7)
    ]
    candidates = [_candidate(unit, 1 - index * 0.01) for index, unit in enumerate(units)]

    many = AdaptiveLegalEvidencePlanner(
        InMemoryLegalEvidenceRepository(
            release=_release(),
            keyword_pages=[candidates],
        ),
        reranker=StaticLegalReranker(
            {unit.unit_id: 0.9 - index * 0.01 for index, unit in enumerate(units)}
        ),
    ).plan(_request())
    few = AdaptiveLegalEvidencePlanner(
        InMemoryLegalEvidenceRepository(
            release=_release(),
            keyword_pages=[candidates],
        ),
        reranker=StaticLegalReranker(
            {
                unit.unit_id: (1.0 if index == 0 else 0.1)
                for index, unit in enumerate(units)
            }
        ),
    ).plan(_request())

    assert len(many.evidence) == 6
    assert len(many.evidence) > 3
    assert len(few.evidence) == 1
    assert len(few.evidence) < 3
    # Retrieval is deliberately not executable until the versioned binder has
    # made evidence-to-check decisions.
    assert many.usable is False
    assert few.usable is False


def test_dynamic_stop_treats_an_already_covered_concept_as_no_new_value() -> None:
    units = [
        _unit("unit-1", "当事人可以依法约定违约金。"),
        _unit("unit-2", "违约金的一般说明。", article_no="第二条"),
        _unit("unit-3", "违约金的其他说明。", article_no="第三条"),
    ]
    repository = InMemoryLegalEvidenceRepository(
        release=_release(),
        keyword_pages=[
            [
                _candidate(units[0], 1.0),
                _candidate(units[1], 0.1),
                _candidate(units[2], 0.09),
            ]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(_request("违约金"))

    assert [item.unit.unit_id for item in bundle.evidence] == ["unit-1"]
    assert bundle.stop_reason == "COVERAGE_SATISFIED"


def test_parallel_issues_receive_one_deterministic_global_safety_budget() -> None:
    candidates = [
        _candidate(
            _unit(f"unit-{index}", f"一般规则{index}", article_no=f"第{index}条"),
            1.0,
        )
        for index in range(1, 5)
    ]
    request = _request("候选中不存在的概念").model_copy(
        update={
            "issues": [
                LegalEvidenceIssue(
                    issue_id=f"legal-issue-{index:032x}",
                    domain="liability_remedies_exit",
                    query=f"并行法规问题{index}",
                    check_codes=["LRE-001"],
                    required_concepts=["候选中不存在的概念"],
                )
                for index in range(1, 8)
            ]
        }
    )
    planner = AdaptiveLegalEvidencePlanner(
        InMemoryLegalEvidenceRepository(
            release=_release(),
            keyword_pages=[candidates],
        ),
        safety=LegalEvidencePlannerSafety(
            maximum_total_examined_candidates=2,
            maximum_total_rounds=2,
        ),
    )

    bundle = planner.plan(request)

    assert bundle.examined_candidate_count == 2
    assert bundle.round_count == 2
    assert bundle.stop_reason == "SAFETY_BUDGET_REACHED"
    assert bundle.usable is False

    repeated = planner.plan(request)
    assert repeated.bundle_hash == bundle.bundle_hash
    assert [item.unit.unit_id for item in repeated.evidence] == [
        item.unit.unit_id for item in bundle.evidence
    ]


def test_requested_jurisdiction_rejects_unknown_candidate_fail_closed() -> None:
    unknown = _unit("unit-unknown", "违约金", article_no="第一条").model_copy(
        update={"jurisdiction": None}
    )
    national = _unit("unit-national", "违约金", article_no="第二条")
    repository = InMemoryLegalEvidenceRepository(
        release=_release(),
        keyword_pages=[
            [_candidate(unknown, 1.0), _candidate(national, 0.9)]
        ],
    )

    bundle = AdaptiveLegalEvidencePlanner(repository).plan(_request("违约金"))

    assert [item.unit.unit_id for item in bundle.evidence] == ["unit-national"]


class _UnexpectedVectorRepository(InMemoryLegalEvidenceRepository):
    def vector_search(self, **_kwargs):
        raise AssertionError("profile mismatch must disable vector search")


class _RecordingEmbeddingProvider:
    profile_id = "configured-profile"

    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, _text: str) -> list[float]:
        self.calls += 1
        return [0.25, 0.75]


def test_embedding_profile_mismatch_disables_vector_and_marks_degraded() -> None:
    embedding = _RecordingEmbeddingProvider()
    repository = _UnexpectedVectorRepository(
        release=_release(embedding_profile_id="indexed-with-another-profile"),
        keyword_pages=[[_candidate(_unit("unit-1", "违约金规则"), 1.0)]],
    )

    bundle = AdaptiveLegalEvidencePlanner(
        repository,
        embedding_provider=embedding,
    ).plan(_request("违约金"))

    assert embedding.calls == 0
    assert bundle.degraded_channels == ["VECTOR"]
    assert bundle.status == "DEGRADED"
    assert bundle.evidence[0].retrieval_channels == ["KEYWORD"]


def test_provider_creates_one_traceable_legal_issue_per_check_with_contract_facts() -> None:
    value = risk_plan_input()
    provider = PlannerLegalEvidenceProvider(
        planner=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
    )

    request = provider._request(value)

    all_check_codes = [code for issue in request.issues for code in issue.check_codes]
    assert len(request.issues) == 45
    assert len(all_check_codes) == len(set(all_check_codes)) == 45
    assert {issue.domain for issue in request.issues} == {
        "formation_validity_authority",
        "commercial_financial",
        "performance_obligations",
        "ip_confidentiality_data",
        "liability_remedies_exit",
        "cross_clause_consistency",
        "missing_ambiguity_completeness",
    }
    assert any(value.source_blocks[0].text in issue.query for issue in request.issues)
    assert all(f"我方：{value.our_party}" in issue.query for issue in request.issues)
    assert all("合同事实：" in issue.query for issue in request.issues)
    assert all("法律议题：" in issue.query for issue in request.issues)
    assert all(len(issue.check_codes) == 1 for issue in request.issues)
    assert all(issue.question and issue.source_domain == issue.domain for issue in request.issues)
    assert all(issue.question != provider.registry.check(issue.check_codes[0]).review_question for issue in request.issues)
    assert all(issue.evidence_need for issue in request.issues)
    assert all(len(issue.required_concepts) <= 3 for issue in request.issues)
    assert all(
        "条件前后冲突" not in issue.required_concepts for issue in request.issues
    )
    assert all(issue.parties == [value.our_party, value.counterparty] for issue in request.issues)


class _SnapshotRepository:
    def __init__(self, snapshot: LegalEvidenceBundle) -> None:
        self.snapshot = snapshot
        self.load_calls: list[tuple[str, str, int, str]] = []

    def load_plan_snapshot(
        self,
        *,
        review_id: str,
        generation_id: str,
        attempt_no: int,
        expected_request_hash: str,
    ) -> LegalEvidencePlanSnapshot:
        self.load_calls.append(
            (review_id, generation_id, attempt_no, expected_request_hash)
        )
        return LegalEvidencePlanSnapshot(
            request_hash=expected_request_hash,
            bundle_hash=self.snapshot.bundle_hash,
            status=self.snapshot.status,
            bundle=self.snapshot,
        )

    def save_plan_snapshot(self, **_kwargs):
        raise AssertionError("a frozen successful snapshot must not be overwritten")

    def save_failed_plan_snapshot(self, **_kwargs):
        raise AssertionError("a frozen successful snapshot must not be overwritten")


class _NeverPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, _request):
        self.calls += 1
        raise AssertionError("planner must not run when a successful snapshot exists")


def test_provider_reuses_successful_snapshot_without_calling_planner() -> None:
    candidate = _candidate(
        _unit("unit-1", "当事人构成违约的，应当承担违约责任。"),
        1.0,
    )
    snapshot = LegalEvidenceCheckBinder().bind(
        AdaptiveLegalEvidencePlanner(
            InMemoryLegalEvidenceRepository(
                release=_release(),
                keyword_pages=[[candidate]],
            )
        ).plan(_request())
    )
    assert snapshot.status == "DEGRADED"
    assert snapshot.unverified_evidence_ids
    assert snapshot.usable is True
    repository = _SnapshotRepository(snapshot)
    planner = _NeverPlanner()
    provider = PlannerLegalEvidenceProvider(
        planner=planner,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
    )

    result = provider.provide(risk_plan_input())

    assert result is snapshot
    assert result.binding_profile_version == LEGAL_EVIDENCE_CHECK_BINDING_VERSION
    assert result.evidence[0].check_codes == ["LRE-001"]
    assert result.usable is True
    assert planner.calls == 0
    assert repository.load_calls == [
        ("review-1", "generation-1", 1, provider._request(risk_plan_input()).stable_hash)
    ]


def test_external_named_article_is_not_duplicated_as_internal_reference() -> None:
    content = "依照《中华人民共和国民法典》第五百八十五条，并按照本法第三条办理。"

    named = extract_named_instrument_references(content)
    internal = extract_internal_references(content)

    assert named[0].target_article_no == "第五百八十五条"
    assert internal == ["第三条"]


def test_semantic_amendment_and_repeal_edges_remain_candidates() -> None:
    references = [
        *extract_named_instrument_references("本规定修改《旧办法》。"),
        *extract_named_instrument_references("自本规定施行之日起废止《旧条例》。"),
    ]
    by_title = {reference.title: reference for reference in references}

    assert by_title["旧办法"].relation_type == "AMENDS"
    assert by_title["旧办法"].verification_status == "CANDIDATE"
    assert by_title["旧条例"].relation_type == "REPEALS"
    assert by_title["旧条例"].verification_status == "CANDIDATE"


def test_migration_guards_active_release_and_published_projection_immutability() -> None:
    sql = (MIGRATIONS_DIR / "006_legal_evidence_projection.sql").read_text("utf-8")

    assert "CREATE UNIQUE INDEX uq_legal_evidence_active_release" in sql
    assert "WHERE status = 'ACTIVE'" in sql
    assert "CREATE FUNCTION legal_evidence_guard_release_identity()" in sql
    assert "published legal evidence releases are immutable" in sql
    assert "invalid legal evidence release state transition" in sql
    assert "OLD.status = 'STAGED' AND NEW.status = 'ACTIVE'" in sql
    assert "OLD.status = 'ACTIVE' AND NEW.status = 'RETIRED'" in sql
    assert "CREATE FUNCTION legal_evidence_guard_published_children()" in sql
    assert "old_release_status IN ('ACTIVE', 'RETIRED')" in sql
    assert "new_release_status IN ('ACTIVE', 'RETIRED')" in sql
    for trigger in (
        "trg_legal_evidence_unit_immutable",
        "trg_legal_evidence_embedding_immutable",
        "trg_legal_evidence_relation_immutable",
    ):
        assert f"CREATE TRIGGER {trigger}" in sql

    forward_sql = (
        MIGRATIONS_DIR / "007_legal_evidence_release_hardening.sql"
    ).read_text("utf-8")
    assert "CREATE OR REPLACE FUNCTION legal_evidence_guard_release_identity()" in forward_sql
    assert "OLD.status = 'RETIRED' AND NEW.status = 'ACTIVE'" in forward_sql
    assert "CREATE OR REPLACE FUNCTION legal_evidence_guard_published_children()" in forward_sql
    assert "old_projection_status = 'READY'" in forward_sql
