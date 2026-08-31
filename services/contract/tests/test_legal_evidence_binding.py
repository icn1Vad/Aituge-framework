from __future__ import annotations

from contract.legal_evidence.binding import (
    LEGAL_EVIDENCE_CHECK_BINDING_VERSION,
    LEGAL_EVIDENCE_CHECK_PROFILES,
    LegalEvidenceCheckBinder,
)
from contract.legal_evidence.models import (
    LegalDomain,
    LegalEvidence,
    LegalEvidenceBundle,
    LegalEvidenceIssue,
    LegalEvidencePlanSnapshot,
    LegalRetrievalUnit,
)
from contract.legal_evidence.prompting import (
    compact_legal_evidence_catalog,
    legal_evidence_ids_for_check,
)
from contract.legal_evidence.provider import PlannerLegalEvidenceProvider
from risk_test_data import risk_plan_input


def _bundle(
    contents: str | list[str],
    *,
    domain: LegalDomain,
    check_codes: list[str],
) -> LegalEvidenceBundle:
    values = [contents] if isinstance(contents, str) else contents
    issue_id = "legal-issue-" + "1" * 32
    evidence = [
        LegalEvidence(
            evidence_id="legal-evidence-" + f"{index:032x}",
            issue_ids=[issue_id],
            # Simulate the former domain-wide assignment. The binder must
            # replace it rather than trust it.
            check_codes=list(check_codes),
            unit=LegalRetrievalUnit(
                unit_id=f"unit-{index}",
                release_id="release-1",
                instrument_id="instrument-1",
                version_id="version-1",
                source_node_ids=[f"node-{index}"],
                title="测试法规",
                article_no=f"第{index}条",
                heading_path=[f"第{index}条"],
                content=content,
                jurisdiction="CN",
                content_hash=f"{index:064x}",
                sequence=index,
            ),
            relevance_score=1,
            retrieval_channels=["KEYWORD"],
        )
        for index, content in enumerate(values, 1)
    ]
    return LegalEvidenceBundle(
        bundle_hash="sha256:" + "a" * 64,
        status="READY",
        release_id="release-1",
        issues=[
            LegalEvidenceIssue(
                issue_id=issue_id,
                domain=domain,
                query="测试法律问题",
                check_codes=check_codes,
            )
        ],
        evidence=evidence,
        relations=[],
        coverage=[],
        unresolved_issue_ids=[],
        conflicts=[],
        degraded_channels=[],
        stop_reason="COVERAGE_SATISFIED",
        examined_candidate_count=len(evidence),
        round_count=len(evidence),
    )


def test_profiles_cover_all_45_checks_and_each_signature_can_bind() -> None:
    assert len(LEGAL_EVIDENCE_CHECK_PROFILES) == 45
    assert len({profile.check_code for profile in LEGAL_EVIDENCE_CHECK_PROFILES}) == 45
    binder = LegalEvidenceCheckBinder()

    for profile in LEGAL_EVIDENCE_CHECK_PROFILES:
        signature = "；".join(group[0] for group in profile.required_term_groups)
        result = binder.bind(
            _bundle(
                signature,
                domain=profile.domain,
                check_codes=[profile.check_code],
            )
        )
        assert result.evidence[0].check_codes == [profile.check_code]


def test_one_commercial_statute_is_not_bound_to_all_eight_domain_checks() -> None:
    result = LegalEvidenceCheckBinder().bind(
        _bundle(
            "经营者应当按照约定开具增值税发票，并明确税费承担和适用税率。",
            domain="commercial_financial",
            check_codes=[f"CF-{number:03d}" for number in range(1, 9)],
        )
    )

    assert result.evidence[0].check_codes == ["CF-003"]
    assert result.binding_status == "PARTIAL"
    assert result.mapped_check_codes == ["CF-003"]
    assert result.unmapped_check_codes == [
        "CF-001", "CF-002", "CF-004", "CF-005", "CF-006", "CF-007", "CF-008"
    ]
    assert result.unmapped_issue_ids == []
    assert result.status == "DEGRADED"
    assert result.unverified_evidence_ids == [result.evidence[0].evidence_id]
    assert "LEGAL_METADATA_UNVERIFIED" in result.evidence[0].cautions


def test_generic_contract_words_fail_closed_without_binding() -> None:
    result = LegalEvidenceCheckBinder().bind(
        _bundle(
            "当事人应当按照合同约定履行义务并及时付款。",
            domain="commercial_financial",
            check_codes=[f"CF-{number:03d}" for number in range(1, 9)],
        )
    )

    assert result.evidence[0].check_codes == []
    assert result.binding_status == "NONE"
    assert result.unmapped_issue_ids == [result.issues[0].issue_id]
    assert result.usable is False


def test_one_statute_can_bind_to_multiple_checks_when_each_has_a_strong_anchor() -> None:
    result = LegalEvidenceCheckBinder().bind(
        _bundle(
            "付款期限届满前，收款方应当开具增值税发票。",
            domain="commercial_financial",
            check_codes=[f"CF-{number:03d}" for number in range(1, 9)],
        )
    )

    assert result.evidence[0].check_codes == ["CF-002", "CF-003"]


def test_unbound_evidence_is_neither_prompted_nor_cited() -> None:
    result = LegalEvidenceCheckBinder().bind(
        _bundle(
            [
                "发票开具应当符合规定。",
                "当事人应当按照合同约定履行义务并及时付款。",
            ],
            domain="commercial_financial",
            check_codes=[f"CF-{number:03d}" for number in range(1, 9)],
        )
    )

    assert [item.check_codes for item in result.evidence] == [["CF-003"], []]
    catalog, _tokens = compact_legal_evidence_catalog(result.evidence)
    assert [item["evidence_id"] for item in catalog] == [
        result.evidence[0].evidence_id
    ]
    assert legal_evidence_ids_for_check(result.evidence, "CF-003") == [
        result.evidence[0].evidence_id
    ]
    assert legal_evidence_ids_for_check(result.evidence, "CF-001") == []


def test_binding_is_versioned_deterministic_and_does_not_cap_matching_count() -> None:
    source = _bundle(
        [f"第{index}项的验收标准和验收程序。" for index in range(1, 13)],
        domain="commercial_financial",
        check_codes=[f"CF-{number:03d}" for number in range(1, 9)],
    )
    first = LegalEvidenceCheckBinder().bind(source)
    second = LegalEvidenceCheckBinder().bind(source)
    next_version = LegalEvidenceCheckBinder(
        profile_version="legal-check-binding-v2-test"
    ).bind(source)

    assert first.binding_profile_version == LEGAL_EVIDENCE_CHECK_BINDING_VERSION
    assert first.bundle_hash == second.bundle_hash
    assert first.bundle_hash != next_version.bundle_hash
    assert len(first.evidence) == 12
    assert all(item.check_codes == ["CF-008"] for item in first.evidence)


class _RecordingPlanner:
    def __init__(self, bundle: LegalEvidenceBundle) -> None:
        self.bundle = bundle
        self.calls = 0

    def plan(self, _request):
        self.calls += 1
        return self.bundle


class _RecordingRepository:
    def __init__(self) -> None:
        self.saved: LegalEvidenceBundle | None = None

    def load_plan_snapshot(self, **_kwargs):
        return None

    def save_plan_snapshot(self, *, bundle: LegalEvidenceBundle, **_kwargs):
        self.saved = bundle
        return LegalEvidencePlanSnapshot(
            request_hash=_kwargs["request"].stable_hash,
            bundle_hash=bundle.bundle_hash,
            status=bundle.status,
            bundle=bundle,
        )

    def save_failed_plan_snapshot(self, **_kwargs):
        raise AssertionError("binding a valid bundle must not fail")


def test_provider_binds_before_freezing_the_snapshot() -> None:
    raw = _bundle(
        "发票开具应当符合规定。",
        domain="commercial_financial",
        check_codes=[f"CF-{number:03d}" for number in range(1, 9)],
    )
    planner = _RecordingPlanner(raw)
    repository = _RecordingRepository()
    provider = PlannerLegalEvidenceProvider(
        planner=planner,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
    )

    result = provider.provide(risk_plan_input())

    assert planner.calls == 1
    assert result.evidence[0].check_codes == ["CF-003"]
    assert result.binding_profile_version == LEGAL_EVIDENCE_CHECK_BINDING_VERSION
    assert repository.saved == result
