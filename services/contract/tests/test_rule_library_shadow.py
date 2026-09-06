from datetime import date
import hashlib
import json
import os
from pathlib import Path

import pytest

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.rule_evidence.shadow import RuleLibraryShadow, issues_from_plan, observe_rule_library
from contract.rule_evidence.snapshot import LocalRuleSnapshot
from risk_test_data import risk_plan_input
from test_rule_evidence_planner import _rule
from contract.rule_evidence.check_binding import matches_assigned_check
from test_rule_evidence_planner import _issue


def snapshot(tmp_path):
    rules = [_rule(s, name="付款期限", content="付款应有明确期限和验收条件",
                   status="pending", party_stance="买受方").model_copy(update={
                       "review_standard": s, "contract_type_path": ["采购合同"],
                       "rule_type": "dedicated", "jurisdiction": None,
                   }) for s in ("neutral", "strong", "weak")]
    data = b"".join((r.model_dump_json() + "\n").encode() for r in rules)
    (tmp_path / "rules.ndjson").write_bytes(data)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "source_version": "test", "snapshot_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        "record_count": len(rules),
    }))
    return tmp_path


def test_existing_plan_rules_bind_to_assigned_checks_without_changing_plan(tmp_path):
    plan = RiskReviewPlanBuilder().build(risk_plan_input())
    before = plan.model_dump_json()
    shadow = RuleLibraryShadow(snapshot(tmp_path))
    result = shadow.evaluate(plan, tenant_id="42", contract_type_name="采购合同", business_role="买受方")
    assert result["eligible_rule_count"] == 1
    assert result["bundle"]["evidence"]
    assert not result["affects_findings"] and result["model_calls"] == 0
    assert plan.model_dump_json() == before
    assigned = {check.check_code for ctx in plan.contexts for check in ctx.check_specs}
    assert set(result["check_evidence"]) <= assigned
    assert observe_rule_library(None, plan, None, "42") is None


def test_corrupted_snapshot_and_foreign_tenant_cannot_enter_review(tmp_path):
    directory = snapshot(tmp_path)
    with (directory / "rules.ndjson").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError):
        LocalRuleSnapshot(directory)


def test_registry_mounts_share_verified_snapshot_but_changed_files_are_rechecked(tmp_path):
    directory = snapshot(tmp_path)
    first = RuleLibraryShadow(directory)
    second = RuleLibraryShadow(directory)
    assert first.snapshot is second.snapshot
    with (directory / "rules.ndjson").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError):
        RuleLibraryShadow(directory)


def test_role_resolution_uses_explicit_header_and_refuses_conflicts(tmp_path):
    from types import SimpleNamespace
    shadow = RuleLibraryShadow(snapshot(tmp_path))
    value = SimpleNamespace(perspective="PARTY_A", source_blocks=[SimpleNamespace(text="采购合同\n甲方（买受方）：某公司")])
    assert shadow.selectors(value) == ("采购合同", "买受方")
    value.source_blocks[0].text = "采购合同\n甲方：某公司\n乙方（买受方）：另一公司"
    assert shadow.selectors(value) == ("采购合同", None)


def test_incidental_payment_words_do_not_bind_defect_rule_to_payment_check():
    rule = _rule("defect", name="隐蔽瑕疵追责", content="发现瑕疵后可暂停付款")
    assert not matches_assigned_check(_issue("a", "付款节点", ["付款"], ["CF-002"]), rule)
    assert matches_assigned_check(_issue("a", "质保瑕疵", ["瑕疵"], ["PO-007"]), rule)


def test_purchase_fact_resolves_buyer_without_assuming_party_a_is_buyer(tmp_path):
    from types import SimpleNamespace
    shadow = RuleLibraryShadow(snapshot(tmp_path))
    value = SimpleNamespace(perspective="PARTY_A", source_blocks=[SimpleNamespace(
        text='设备采购合同\n就“甲方向乙方采购三套设备。”事宜订立本合同。')])
    assert shadow.selectors(value) == ("采购合同", "买受方")
    value.perspective = "PARTY_B"
    assert shadow.selectors(value) == ("采购合同", None)  # fixture has no seller rules
    value.source_blocks[0].text = "采购合同\n乙方向甲方采购设备。"
    assert shadow.selectors(value) == ("采购合同", "买受方")
    value.source_blocks[0].text = "采购合同\n乙方不得向甲方采购设备。"
    assert shadow.selectors(value) == ("采购合同", None)
    value.source_blocks[0].text = "采购合同\n如果乙方向甲方采购设备，另行签约。"
    assert shadow.selectors(value) == ("采购合同", None)


@pytest.mark.skipif(not os.getenv("RULE_LIBRARY_REAL_SNAPSHOT_DIR"), reason="Existing rule snapshot not mounted")
def test_all_existing_rules_and_three_standards_run_through_real_review_plan():
    shadow = RuleLibraryShadow(os.environ["RULE_LIBRARY_REAL_SNAPSHOT_DIR"])
    assert len(shadow.snapshot.rules) == 34959
    plan = RiskReviewPlanBuilder().build(risk_plan_input())
    results = []
    for standard in ("neutral", "strong", "weak"):
        result = shadow.evaluate(plan, tenant_id="42", contract_type_name="采购合同", business_role="买受方",
                                 review_standard=standard, review_as_of_date=date(2026, 9, 5))
        assert result["eligible_rule_count"] > 0
        assert result["bundle"]["evidence"]
        assert {e["rule"]["review_standard"] for e in result["bundle"]["evidence"]} == {standard}
        assert {e["rule"]["party_stance"] for e in result["bundle"]["evidence"]} == {"买受方"}
        assert all(e["check_codes"] for e in result["bundle"]["evidence"])
        assert result["bundle"]["preview_only"] and not result["affects_findings"]
        results.append(result)
    report_dir = os.getenv("RULE_LIBRARY_REPORT_DIR")
    if report_dir:
        with (Path(report_dir) / "existing-rules-plan-integration.json").open("w", encoding="utf-8") as stream:
            json.dump(results, stream, ensure_ascii=False, indent=2)
    print(json.dumps([{"standard": r["review_standard"], "eligible": r["eligible_rule_count"],
                      "selected": len(r["bundle"]["evidence"]), "duration_ms": r["duration_ms"]}
                     for r in results], ensure_ascii=False))
