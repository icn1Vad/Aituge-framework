from datetime import date
import pytest

from contract.evidence_planning import EvidencePlanningEngine, EvidencePlanningProfile
from contract.rule_evidence import (
    AdaptiveRuleEvidencePlanner,
    JavaRuleLibrarySnapshot,
    ReviewRuleSnapshot,
    RuleEvidenceBinder,
    RuleEvidenceIssue,
    RuleEvidencePlanRequest,
    RuleLibraryRelation,
)


def _issue(
    suffix: str,
    query: str,
    concepts: list[str],
    check_codes: list[str] | None = None,
) -> RuleEvidenceIssue:
    return RuleEvidenceIssue(
        issue_id="rule-issue-" + suffix * 32,
        domain="commercial_financial",
        query=query,
        required_concepts=concepts,
        check_codes=check_codes or ["CF-001"],
    )


def _rule(
    rule_id: str,
    *,
    name: str,
    content: str,
    tenant_id: str = "0",
    status: str = "active",
    rule_type: str = "general",
    contract_type_id: str | None = None,
    party_stance: str | None = "neutral",
    jurisdiction: str | None = "CN",
    effective_from: date | None = None,
    effective_to: date | None = None,
    target_check_codes: list[str] | None = None,
) -> ReviewRuleSnapshot:
    return ReviewRuleSnapshot(
        rule_id=rule_id,
        code="RR-" + rule_id,
        version=1,
        tenant_id=tenant_id,
        review_direction=name,
        name=name,
        contract_type_id=contract_type_id,
        party_stance=party_stance,
        review_standard="neutral",
        rule_type=rule_type,
        source="user",
        content=content,
        review_method="核对合同正文并引用命中条款",
        status=status,
        jurisdiction=jurisdiction,
        effective_from=effective_from,
        effective_to=effective_to,
        target_check_codes=target_check_codes or [],
    )


def _request(
    issues: list[RuleEvidenceIssue],
    rules: list[ReviewRuleSnapshot],
    *,
    relations: list[RuleLibraryRelation] | None = None,
) -> RuleEvidencePlanRequest:
    return RuleEvidencePlanRequest(
        review_id="review-1",
        generation_id="generation-1",
        tenant_id="42",
        contract_type="SOFTWARE_SERVICE",
        perspective="PARTY_A",
        jurisdiction="CN-11",
        review_as_of_date=date(2026, 9, 5),
        source_version="java-review-rule-snapshot-v1",
        issues=issues,
        rules=rules,
        relations=relations or [],
    )


def _engine() -> EvidencePlanningEngine:
    engine = EvidencePlanningEngine()
    engine.register(
        EvidencePlanningProfile(
            profile_id="rule-library",
            planner=AdaptiveRuleEvidencePlanner(),
            binder=RuleEvidenceBinder(),
        )
    )
    return engine


@pytest.mark.parametrize("standard", ["neutral", "strong", "weak"])
def test_standard_selection_never_mixes_negotiating_positions(standard):
    rules = [_rule(value, name="付款期限", content="约定付款期限").model_copy(
        update={"review_standard": value}) for value in ("neutral", "strong", "weak")]
    request = _request([_issue("a", "付款期限", ["付款"])], rules).model_copy(
        update={"review_standard": standard})
    bundle = _engine().execute("rule-library", request)
    assert {item.rule.review_standard for item in bundle.evidence} == {standard}


def test_pending_preview_preserves_source_status_and_is_not_usable():
    rule = _rule("pending", name="付款期限", content="付款期限", status="pending", party_stance="买受方")
    request = _request([_issue("b", "付款期限", ["付款"])], [rule]).model_copy(
        update={"business_role": "买受方"})
    assert not _engine().execute("rule-library", request).evidence
    preview = _engine().execute("rule-library", request.model_copy(update={"preview_pending": True}))
    assert preview.evidence[0].rule.status == "pending"
    assert preview.preview_only and not preview.usable
    wrong_role = request.model_copy(update={"preview_pending": True, "business_role": "出卖方"})
    assert not _engine().execute("rule-library", wrong_role).evidence


def test_dedicated_type_is_exact_and_empty_type_cannot_match_everything():
    rule = _rule("type", name="付款期限", content="付款期限", rule_type="dedicated",
                 contract_type_id="SOFTWARE_SERVICE")
    request = _request([_issue("c", "付款期限", ["付款"])], [rule])
    assert not _engine().execute("rule-library", request.model_copy(update={"contract_type": "SERVICE"})).evidence


def test_rule_library_profile_filters_scope_status_dates_stance_and_contract_type() -> None:
    matching = _rule("matching", name="付款期限", content="付款期限不得超过三十日")
    matching_dedicated = _rule(
        "dedicated",
        name="软件验收",
        content="软件项目应约定验收标准",
        rule_type="dedicated",
        contract_type_id="SOFTWARE_SERVICE",
    )
    rejected = [
        _rule("inactive", name="付款期限", content="付款期限", status="expired"),
        _rule("tenant", name="付款期限", content="付款期限", tenant_id="99"),
        _rule(
            "future",
            name="付款期限",
            content="付款期限",
            effective_from=date(2027, 1, 1),
        ),
        _rule("region", name="付款期限", content="付款期限", jurisdiction="CN-31"),
        _rule("stance", name="付款期限", content="付款期限", party_stance="PARTY_B"),
        _rule(
            "wrong-type",
            name="付款期限",
            content="付款期限",
            rule_type="dedicated",
            contract_type_id="HOUSE_LEASE",
        ),
    ]
    request = _request(
        [_issue("a", "软件服务的付款期限和验收标准是否清楚", ["付款", "验收"])],
        [matching, matching_dedicated, *rejected],
    )

    bundle = _engine().execute("rule-library", request)

    assert bundle.usable is True
    assert bundle.status == "READY"
    assert {item.rule.rule_id for item in bundle.evidence} == {"matching", "dedicated"}
    assert {code for item in bundle.evidence for code in item.check_codes} == {"CF-001"}
    assert bundle.snapshot_hash == request.snapshot_hash


def test_rule_profile_allows_zero_evidence_without_filling_a_quota() -> None:
    request = _request(
        [_issue("b", "量子卫星轨道保险如何处理", ["量子卫星"])],
        [_rule("payment", name="付款期限", content="付款期限不得超过三十日")],
    )

    bundle = _engine().execute("rule-library", request)

    assert bundle.status == "NO_RELEVANT_EVIDENCE"
    assert bundle.binding_status == "NONE"
    assert bundle.evidence == []
    assert bundle.usable is False


def test_verified_rule_relation_expands_to_evidence_outside_initial_recall() -> None:
    source = _rule("source", name="付款期限", content="付款期限不得超过三十日")
    exception = _rule(
        "exception",
        name="重大项目审批例外",
        content="超过预算阈值时必须取得董事会书面批准",
    )
    relation = RuleLibraryRelation(
        relation_id="relation-1",
        source_rule_id="source",
        target_rule_id="exception",
        relation_type="EXCEPTION_TO",
        evidence_text="付款期限规则对重大项目受董事会审批规则约束",
        confidence=1.0,
        verification_status="VERIFIED",
    )
    request = _request(
        [_issue("c", "付款期限是否明确", ["付款"])],
        [source, exception],
        relations=[relation],
    )

    bundle = _engine().execute("rule-library", request)

    assert {item.rule.rule_id for item in bundle.evidence} == {"source", "exception"}
    expanded = next(item for item in bundle.evidence if item.rule.rule_id == "exception")
    assert expanded.retrieval_channels == ["RELATION"]
    assert expanded.relation_path == ["relation-1"]
    assert [item.relation_id for item in bundle.relations] == ["relation-1"]


def test_target_check_codes_fail_closed_during_binding() -> None:
    request = _request(
        [_issue("d", "付款义务是否明确", ["付款"], ["CF-001", "CF-002"])],
        [
            _rule(
                "targeted",
                name="付款义务",
                content="应明确付款义务",
                target_check_codes=["CF-002", "FVA-001"],
            )
        ],
    )

    bundle = _engine().execute("rule-library", request)

    assert bundle.evidence[0].check_codes == ["CF-002"]
    assert bundle.binding_status == "COMPLETE"


def test_snapshot_hash_changes_when_a_rule_version_changes() -> None:
    issue = _issue("e", "付款期限", ["付款"])
    first = _request([issue], [_rule("payment", name="付款", content="三十日内付款")])
    changed_rule = _rule("payment", name="付款", content="六十日内付款")
    changed_rule.version = 2
    second = _request([issue], [changed_rule])

    assert first.snapshot_hash != second.snapshot_hash


def test_java_snapshot_wire_contract_is_consumed_without_rehashing() -> None:
    supplied_hash = "sha256:" + "1" * 64
    snapshot = JavaRuleLibrarySnapshot.model_validate(
        {
            "schema_version": "1.0",
            "source_version": "java-review-rule-snapshot-v1",
            "snapshot_hash": supplied_hash,
            "tenant_id": "42",
            "as_of_date": "2026-09-05",
            "rules": [
                _rule("payment", name="付款期限", content="付款期限不得超过三十日")
                .model_copy(update={"tenant_id": "42"})
                .model_dump(mode="json")
            ],
        }
    )
    request = snapshot.planning_request(
        review_id="review-1",
        generation_id="generation-1",
        contract_type="SOFTWARE_SERVICE",
        perspective="PARTY_A",
        jurisdiction="CN-11",
        contract_date=None,
        issues=[_issue("f", "付款期限是否明确", ["付款"])],
    )

    bundle = _engine().execute("rule-library", request)

    assert bundle.snapshot_hash == supplied_hash
    assert bundle.evidence[0].rule.rule_id == "payment"
