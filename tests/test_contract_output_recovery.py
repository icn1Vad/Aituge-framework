"""Offline regression matrix for check-local recovery. All completions are fake."""
import copy
import json
import os
import stat
from types import SimpleNamespace

import pytest

from test_contract_risk_direct_review import _request, _request_with_single_payment_text, _valid_payload, _completion, _review
from services.contract.capabilities.risk_review import DirectReviewError, _parse_model_output, _prompt
from services.contract.capabilities.risk_review_bundle import _failed_batch_result
from services.contract.capabilities.review_output_diagnostics import write_private_diagnostic


@pytest.mark.parametrize("missing", [None, "", "   ", "MISSING"])
def test_only_missing_explanation_is_repaired_and_other_checks_stay_identical(missing):
    initial = _valid_payload()
    check = initial["check_results"][1]
    if missing == "MISSING":
        check.pop("decision_note")
    else:
        check["decision_note"] = missing
    repaired = copy.deepcopy(check)
    repaired["decision_note"] = "付款约定为验收后十日内支付，当前条款没有履约前付款问题。"
    result, runtime = _review([_completion(json.dumps(initial, ensure_ascii=False)),
                               _completion(json.dumps({"check_results": [repaired]}, ensure_ascii=False), repair_no=1)])
    assert result.model_call_count == 2 and result.repair_count == 1
    repair = json.loads(runtime.calls[1]["messages"][0]["content"])
    assert repair["target_check_codes"] == ["CF-002"]
    assert len(repair["original_checks"]) == 1
    assert [item["check_code"] for item in repair["context"]["assigned_check_specs"]] == ["CF-002"]
    assert result.attempt_diagnostics[1].repair_check_codes == ["CF-002"]
    normalized = result.attempt_diagnostics[1].normalized_output
    for index in range(8):
        if index != 1:
            from test_seven_domain_evidence_protocol import normalized_fixture
            assert normalized["check_results"][index] == normalized_fixture(initial)["check_results"][index]
    assert result.check_results[1].decision_note == repaired["decision_note"]


def test_targeted_repair_may_correct_an_existing_nonblank_explanation():
    initial = _valid_payload()
    check = initial["check_results"][1]
    check["decision_note"] = "付款时间需要核对。"
    repaired = copy.deepcopy(check)
    repaired["decision_note"] = "依据提供的付款条款，付款时间应按验收后的约定判断。"
    check.pop("status")  # A schema error targets this check for local repair.
    result, runtime = _review([
        _completion(json.dumps(initial, ensure_ascii=False)),
        _completion(json.dumps({"check_results": [repaired]}, ensure_ascii=False), repair_no=1),
    ])
    assert result.check_results[1].decision_note == repaired["decision_note"]
    assert result.attempt_diagnostics[-1].semantic_preservation_passed is True
    assert json.loads(result.attempt_diagnostics[0].raw_content)["check_results"][1]["decision_note"] == check["decision_note"]
    payload = json.loads(runtime.calls[1]["messages"][0]["content"])
    assert payload["target_check_codes"] == ["CF-002"]
    assert any("包括已有非空说明" in rule for rule in payload["constraints"])
    normalized = result.attempt_diagnostics[-1].normalized_output["check_results"]
    from test_seven_domain_evidence_protocol import normalized_fixture
    siblings = {"check_results": [value for i,value in enumerate(initial["check_results"]) if i != 1]}
    assert [value for i,value in enumerate(normalized) if i != 1] == normalized_fixture(siblings)["check_results"]


def test_explanation_permission_does_not_allow_changes_to_untargeted_checks():
    initial = _valid_payload()
    initial["check_results"][1]["decision_note"] = ""
    repaired = copy.deepcopy(initial)
    repaired["check_results"][1]["decision_note"] = "依据付款条款补充判断理由。"
    repaired["check_results"][0]["decision_note"] = "不应修改已通过的其他检查项。"
    with pytest.raises(DirectReviewError) as caught:
        _review([
            _completion(json.dumps(initial, ensure_ascii=False)),
            _completion(json.dumps(repaired, ensure_ascii=False), repair_no=1),
        ])
    assert caught.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"
    assert "untargeted check" in str(caught.value)


def test_empty_note_does_not_hide_an_independent_bad_reference():
    initial = _valid_payload()
    initial["check_results"][1]["decision_note"] = ""
    initial["check_results"][0]["findings"][0]["evidence"][0]["evidence_ref"] = "A999"
    _, ir, anchors, candidate = _prompt(_request())
    with pytest.raises(DirectReviewError) as caught:
        _parse_model_output(json.dumps(initial), ir_refs=ir, anchor_refs=anchors, cf005_candidate=candidate)
    error = caught.value
    assert {item["stage"] for item in error.validation_issues} >= {"SCHEMA", "EVIDENCE"}
    assert error.check_codes == ["CF-001", "CF-002"]
    assert error.normalized_output["check_results"][1]["decision_note"] == ""
    assert error.normalized_output["check_results"][0]["findings"][0]["evidence"][0]["evidence_ref"] == "A999"


def test_repair_cannot_change_an_untargeted_passed_check():
    initial = _valid_payload()
    initial["check_results"][1]["decision_note"] = ""
    repaired = copy.deepcopy(initial)
    repaired["check_results"][1]["decision_note"] = "依据验收后付款条款完成判断。"
    repaired["check_results"][0]["findings"] = []
    with pytest.raises(DirectReviewError) as caught:
        _review([_completion(json.dumps(initial)), _completion(json.dumps(repaired), repair_no=1)])
    assert caught.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"
    assert len(caught.value.attempt_diagnostics) == 2


@pytest.mark.parametrize("perspective", ["PARTY_A", "PARTY_B"])
def test_payer_payee_reversal_does_not_require_invented_safeguards(perspective):
    request = _request_with_single_payment_text("合同签订后十日内，甲方一次性支付全部合同价款。")
    if perspective == "PARTY_B":
        request = request.model_copy(update={"perspective": "PARTY_B", "our_party": "乙方单位", "counterparty": "甲方单位"})
    initial = _valid_payload(finding_count=0)
    initial["check_results"][4].update(candidate_decision="TRIGGER_NOT_MET", identified_security_mechanisms=[], candidate_evidence=[],
                                      decision_note="本次我方是收款方，不属于我方承担的预付款风险。")
    if perspective == "PARTY_B":
        result, runtime = _review([_completion(json.dumps(initial))], request)
        assert result.status == "COMPLETED" and len(runtime.calls) == 1
    else:
        with pytest.raises(DirectReviewError) as caught:
            _review([_completion(json.dumps(initial)), _completion(json.dumps(initial), repair_no=1)], request)
        assert caught.value.code == "RISK_CF005_APPLICABILITY_INVALID"


def test_legacy_no_risk_without_safeguard_maps_only_for_verified_payee():
    request = _request_with_single_payment_text("合同签订后十日内，甲方一次性支付全部合同价款。")
    request = request.model_copy(update={"perspective": "PARTY_B", "our_party": "乙方单位", "counterparty": "甲方单位"})
    initial = _valid_payload(finding_count=0)
    initial["check_results"][4].update(candidate_decision="RISK_NOT_CONFIRMED", identified_security_mechanisms=[], candidate_evidence=[])
    result, runtime = _review([_completion(json.dumps(initial))], request)
    assert result.status == "COMPLETED" and len(runtime.calls) == 1
    assert result.attempt_diagnostics[0].normalized_output["check_results"][4]["candidate_decision"] == "TRIGGER_NOT_MET"


@pytest.mark.parametrize("text, expected", [
    ("甲方在交付完成后十日内支付合同价款。", True),
    ("甲方在签订合同后一次性支付全部合同价款，乙方验收后移交资料。", False),
])
def test_no_prepayment_requires_explicit_payment_context(text, expected):
    request = _request_with_single_payment_text(text)
    _, _, _, candidate = _prompt(request)
    assert candidate.trigger_absence_verified is expected
    if expected:
        initial = _valid_payload(finding_count=0)
        initial["check_results"][4].update(candidate_decision="TRIGGER_NOT_MET", identified_security_mechanisms=[], candidate_evidence=[])
        result, _ = _review([_completion(json.dumps(initial))], request)
        assert result.status == "COMPLETED"


def test_unknown_payer_remains_unknown_not_inapplicable():
    request = _request_with_single_payment_text("应在合同签订后一次性支付全部合同价款。")
    request.projected_ir_items[0].subject = "未明确主体"
    initial = _valid_payload(finding_count=0)
    initial["check_results"][4].update(candidate_decision="TRIGGER_NOT_MET", identified_security_mechanisms=[], candidate_evidence=[])
    with pytest.raises(DirectReviewError) as caught:
        _review([_completion(json.dumps(initial)), _completion(json.dumps(initial), repair_no=1)], request)
    assert caught.value.code == "RISK_CF005_APPLICABILITY_INVALID"


def test_insufficient_evidence_stays_pending_without_blocking_other_batches():
    initial = _valid_payload(finding_count=0)
    initial["check_results"][4].update(status="FAILED", candidate_decision="INSUFFICIENT_EVIDENCE",
                                      identified_security_mechanisms=[], candidate_evidence=[])
    result, _ = _review([_completion(json.dumps(initial))])
    assert result.status == "PARTIAL_FAILED"
    assert result.check_results[4].status == "FAILED"
    assert result.check_results[4].reason_code == "INSUFFICIENT_EVIDENCE"


def test_failed_calls_keep_usage_and_private_full_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRACT_REVIEW_DIAGNOSTIC_DIR", str(tmp_path / "private"))
    initial = _valid_payload()
    for check in initial["check_results"]:
        check["decision_note"] = ""
    with pytest.raises(DirectReviewError) as caught:
        _review([_completion(json.dumps(initial)), _completion(json.dumps(initial), repair_no=1)])
    error = caught.value
    assert error.diagnostic_id
    record_path = next((tmp_path / "private").glob("*.json"))
    record = json.loads(record_path.read_text())
    assert len(record["payload"]["attempts"]) == 2
    assert len(record["payload"]["attempts"][0]["validation_issues"]) >= 8
    from test_seven_domain_evidence_protocol import wire_fixture, catalog_for
    assert json.loads(record["payload"]["attempts"][0]["raw_content"]) == wire_fixture(initial, catalog_for(_request()))
    if os.name != "nt":  # Production is Linux; Windows uses ACLs, not POSIX mode bits.
        assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    if os.name != "nt":
        assert stat.S_IMODE(record_path.parent.stat().st_mode) == 0o700
    request = _request()
    batch = _failed_batch_result(SimpleNamespace(unit_id="commercial_financial", batch_id=request.batch_id,
                                                check_specs=request.assigned_check_specs), error, duration_ms=10)
    assert batch.status == "FAILED" and batch.findings == []
    assert batch.model_call_count == 2 and batch.repair_count == 1
    assert batch.prompt_tokens == 5600 and batch.completion_tokens == 1200
    assert len(batch.attempt_diagnostics) == 2
    assert batch.warnings[0].startswith("LOCAL_DIAGNOSTIC:")


def test_local_diagnostic_redacts_runtime_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRACT_REVIEW_DIAGNOSTIC_DIR", str(tmp_path))
    monkeypatch.setenv("EXAMPLE_API_KEY", "test-secret-not-a-real-key")
    write_private_diagnostic("test", {}, {"raw": "test-secret-not-a-real-key Bearer abcdef123456"})
    text = next(tmp_path.glob("*.json")).read_text()
    assert "test-secret-not-a-real-key" not in text and "abcdef123456" not in text


def test_installment_semantics_do_not_require_the_literal_word_in_quote():
    request = _request_with_single_payment_text(
        "首次货品交付且经甲方验收合格后支付价款25%；第二次交付且经甲方验收合格后再支付价款25%。")
    request.projected_ir_items[0].predicate = "分期支付"
    initial = _valid_payload(finding_count=0)
    initial["check_results"][4]["identified_security_mechanisms"] = ["INSTALLMENT_PAYMENT", "ACCEPTANCE_LINKAGE"]
    assert "分期" not in request.source_excerpts[0].quoted_text
    result, _ = _review([_completion(json.dumps(initial))], request)
    assert result.status == "COMPLETED"


def test_invented_safeguard_is_still_rejected():
    initial = _valid_payload(finding_count=0)
    initial["check_results"][4]["identified_security_mechanisms"] = ["INVENTED_SAFEGUARD"]
    with pytest.raises(DirectReviewError) as caught:
        _review([_completion(json.dumps(initial)), _completion(json.dumps(initial), repair_no=1)])
    assert caught.value.code == "RISK_CF005_SAFEGUARD_EVIDENCE_INVALID"
