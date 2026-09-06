from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from pydantic import ValidationError
from service.conversation.llm_runner import LlmCompletionResult

from services.contract.capabilities.risk_review import (
    COMMERCIAL_CHECK_CODES,
    COMMERCIAL_DECISION_POLICIES,
    CommercialFinancialDirectReviewer,
    CommercialReviewRequest,
    DirectReviewError,
    ModelCheckCoverageResult,
    _enrich_reason_codes,
    _parse_model_output,
    _prompt,
)


def _request() -> CommercialReviewRequest:
    quotes = [f"第{index}期款项应在验收后十日内支付。" for index in range(1, 6)]
    risk_types = {
        "CF-001": "PRICE_CALCULATION_RISK",
        "CF-002": "PAYMENT_TIMING_RISK",
        "CF-003": "INVOICE_TAX_CONDITION_RISK",
        "CF-004": "PAYMENT_ADJUSTMENT_RISK",
        "CF-005": "ADVANCE_PAYMENT_SECURITY_RISK",
        "CF-006": "PAYMENT_PATH_RISK",
        "CF-007": "DELIVERY_RISK",
        "CF-008": "ACCEPTANCE_RISK",
    }
    categories = {
        **{code: "PAYMENT" for code in COMMERCIAL_CHECK_CODES[:6]},
        "CF-007": "DELIVERY",
        "CF-008": "ACCEPTANCE",
    }
    return CommercialReviewRequest(
        review_id="review-direct-1",
        document_id="document-direct-1",
        generation_id="generation-direct-1",
        attempt_no=1,
        plan_id="risk-plan-" + "1" * 32,
        context_hash="sha256:" + "2" * 64,
        unit_id="commercial_financial",
        batch_id="risk-batch-" + "3" * 32,
        perspective="PARTY_A",
        our_party="甲方单位",
        counterparty="乙方单位",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        assigned_check_specs=[
            {
                "check_code": code,
                "review_question": f"检查{code}",
                "allowed_categories": [categories[code]],
                "allowed_risk_types": [risk_types[code]],
                "criticality": "REQUIRED",
            }
            for code in COMMERCIAL_CHECK_CODES
        ],
        definitions=[],
        projected_ir_items=[
            {
                "ir_type": "payment_terms",
                "item_id": f"ir-payment-{index}",
                "subject": "甲方",
                "predicate": "支付",
                "object": quote,
                "source_anchors": [{"anchor_id": f"anchor-commercial-{index}"}],
            }
            for index, quote in enumerate(quotes, start=1)
        ],
        source_excerpts=[
            {
                "anchor_id": f"anchor-commercial-{index}",
                "block_id": f"block-commercial-{index}",
                "block_no": index,
                "page_number": None,
                "char_start": 0,
                "char_end": len(quote),
                "quoted_text": quote,
                "quoted_text_hash": (
                    "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
                ),
                "heading_path": ["付款"],
            }
            for index, quote in enumerate(quotes, start=1)
        ],
        estimated_input_tokens=2800,
    )


def _valid_payload(*, finding_count: int = 1) -> dict:
    results = []
    for code in COMMERCIAL_CHECK_CODES:
        findings = []
        if code == "CF-001":
            findings = [
                {
                    "check_code": code,
                    "category": "PAYMENT",
                    "risk_type": "PRICE_CALCULATION_RISK",
                    "risk_level": "HIGH",
                    "title": f"价款风险{index}",
                    "issue": f"价款口径需要进一步明确{index}",
                    "impact_to_our_party": "可能增加我方付款争议",
                    "suggestion": "明确总价及计价范围",
                    "evidence": [
                        {
                            "evidence_type": "TEXT_QUOTE",
                            "ir_ref": f"I{index + 1:03d}",
                            "evidence_ref": f"A{index + 1:03d}",
                            "checked_scope": None,
                            "verification_note": None,
                        }
                    ],
                }
                for index in range(finding_count)
            ]
        result = {
                "check_code": code,
                "status": "REVIEWED",
                "reason_code": "MATERIAL_RISK_FOUND" if findings else "NO_MATERIAL_RISK",
                "decision_note": (
                    "识别到对我方不利的实质风险"
                    if findings
                    else "已完成检查且未发现对我方不利的实质风险"
                ),
                "findings": findings,
            }
        if code == "CF-005":
            result.update(
                {
                    "candidate_decision": "RISK_NOT_CONFIRMED",
                    "identified_security_mechanisms": ["ACCEPTANCE_LINKAGE"],
                    "candidate_evidence": [
                        {
                            "evidence_type": "TEXT_QUOTE",
                            "ir_ref": "I001",
                            "evidence_ref": "A001",
                            "checked_scope": None,
                            "verification_note": None,
                        }
                    ],
                }
            )
        results.append(result)
    return {"check_results": results}


def _completion(
    content: str,
    *,
    repair_no: int = 0,
    prompt_tokens: int = 2800,
) -> LlmCompletionResult:
    return LlmCompletionResult(
        content=content,
        prompt_tokens=prompt_tokens,
        cached_tokens=min(prompt_tokens, 1200),
        completion_tokens=600,
        total_tokens=prompt_tokens + 600,
        time_to_first_token_ms=300,
        model_duration_ms=1800,
        trace_id=f"trace-{repair_no}",
        provider_request_id=f"request-{repair_no}",
        finish_reason="stop",
        review_unit_id="commercial_financial",
        review_id="review-direct-1",
        framework_run_id="run-1",
        attempt_no=1,
        repair_no=repair_no,
    )


class FakeRuntime:
    def __init__(self, responses: list[LlmCompletionResult]) -> None:
        self.responses = responses
        self.calls = []

    async def complete_with_usage(self, **kwargs) -> LlmCompletionResult:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _review(
    responses: list[LlmCompletionResult],
    request: CommercialReviewRequest | None = None,
):
    runtime = FakeRuntime(responses)
    reviewer = CommercialFinancialDirectReviewer(runtime_factory=lambda _tenant: runtime)
    result = asyncio.run(
        reviewer.review(
            request or _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            framework_run_id="run-1",
        )
    )
    return result, runtime


def test_empty_legal_evidence_keeps_commercial_prompt_on_legacy_shape() -> None:
    prompt, _ir_refs, _anchor_refs, _candidate = _prompt(_request())
    payload = json.loads(prompt.split("\n", 1)[1])
    assert "legal_evidence_catalog" not in payload
    assert "legal_evidence_input_tokens" not in payload
    assert not any(
        "legal_evidence" in rule
        for rule in payload["output_contract"]["rules"]
    )


def test_legal_catalog_budget_omission_is_explicit(monkeypatch) -> None:
    request = _request()
    request.legal_evidence = [object()]  # compactor is isolated below
    monkeypatch.setattr(
        "services.contract.capabilities.risk_review.compact_legal_evidence_catalog",
        lambda *_args, **_kwargs: ([], 0),
    )

    _prompt(request)

    assert len(request.legal_evidence) == 1
    assert request.legal_evidence_prompt_status == "OMITTED_TOKEN_BUDGET"


def test_direct_review_succeeds_with_all_checks_zero_tools_and_one_call() -> None:
    result, runtime = _review([_completion(json.dumps(_valid_payload(), ensure_ascii=False))])

    assert result.status == "COMPLETED"
    assert [item.check_code for item in result.check_results] == list(COMMERCIAL_CHECK_CODES)
    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.tool_call_count == 0
    assert result.schema_normalization_applied is False
    assert result.schema_normalization_type is None
    assert len(result.attempt_diagnostics) == 1
    assert result.attempt_diagnostics[0].raw_content
    assert result.reason_code_enrichment_count == 8
    assert result.reason_code_rule_version == "1.0"
    assert result.ignored_model_reason_code_count == 8
    assert result.check_results[0].reason_code == "RISK_IDENTIFIED"
    assert result.check_results[1].reason_code == "NO_RISK_IDENTIFIED"
    assert result.prompt_tokens == 2800
    assert result.cached_tokens == 1200
    assert result.completion_tokens == 600
    assert result.call_metrics[0].review_unit_id == "commercial_financial"
    assert len(result.findings) == 1
    evidence = result.findings[0].evidence_candidates[0]
    assert evidence.source_ir_item_id == "ir-payment-1"
    assert evidence.block_id == "block-commercial-1"
    assert evidence.quoted_text == "第1期款项应在验收后十日内支付。"
    assert runtime.calls[0]["temperature"] == 0
    assert runtime.calls[0]["thinking_override"] is False
    assert runtime.calls[0]["response_format"] == {"type": "json_object"}
    assert runtime.calls[0]["review_unit_id"] == "commercial_financial"
    assert "tools" not in runtime.calls[0]
    prompt_payload = json.loads(runtime.calls[0]["messages"][0]["content"].split("\n", 1)[1])
    assert "assigned_check_specs" in prompt_payload
    assert "checks" not in prompt_payload


def test_large_provider_prompt_preserves_valid_result_without_schema_repair() -> None:
    runtime = FakeRuntime(
        [
            _completion(
                json.dumps(_valid_payload(), ensure_ascii=False),
                prompt_tokens=17694,
            ),
            _completion(
                json.dumps(_valid_payload(), ensure_ascii=False),
                repair_no=1,
            ),
        ]
    )
    reviewer = CommercialFinancialDirectReviewer(
        runtime_factory=lambda _tenant: runtime
    )

    result = asyncio.run(
        reviewer.review(
            _request(),
            tenant_id="tenant-1",
            model_id="deepseek-v4-flash",
            framework_run_id="run-1",
        )
    )
    assert result.status == "COMPLETED"
    assert result.findings
    assert result.prompt_tokens == 17694
    assert result.repair_count == 0
    assert len(runtime.calls) == 1
    assert len(runtime.responses) == 1


def test_direct_review_does_not_apply_legacy_four_finding_cap() -> None:
    result, _runtime = _review(
        [_completion(json.dumps(_valid_payload(finding_count=5), ensure_ascii=False))]
    )

    assert len(result.findings) == 5


def test_valid_7035_token_response_is_preserved_without_another_call() -> None:
    result, runtime = _review(
        [_completion(json.dumps(_valid_payload(finding_count=5), ensure_ascii=False), prompt_tokens=7035)]
    )
    assert len(result.findings) == 5
    assert len(runtime.calls) == 1
    assert result.prompt_tokens == 7035


def test_empty_findings_are_valid_when_all_checks_are_covered() -> None:
    payload = _valid_payload(finding_count=0)
    result, _runtime = _review([_completion(json.dumps(payload, ensure_ascii=False))])

    assert result.findings == []
    assert all(item.status == "REVIEWED" for item in result.check_results)


def test_invalid_json_is_repaired_once_for_current_unit() -> None:
    result, runtime = _review(
        [
            _completion("not-json"),
            _completion(json.dumps(_valid_payload(), ensure_ascii=False), repair_no=1),
        ]
    )

    assert result.model_call_count == 2
    assert result.repair_count == 1
    assert result.repair_reasons[0].startswith("RISK_DIRECT_SCHEMA_INVALID:")
    assert runtime.calls[1]["repair_no"] == 1
    assert len(runtime.calls[1]["messages"]) == 1
    repair_request = json.loads(runtime.calls[1]["messages"][0]["content"])
    assert repair_request["first_raw_json"] == "not-json"
    assert "不得把非空findings改为空数组" in repair_request["constraints"]


def test_persistently_invalid_json_fails_after_one_repair() -> None:
    with pytest.raises(DirectReviewError, match="output is invalid"):
        _review([_completion("bad"), _completion("still-bad", repair_no=1)])


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda body: body["check_results"].pop(),
            "RISK_CHECK_COVERAGE_INVALID",
        ),
        (
            lambda body: body["check_results"].__setitem__(
                7, {**body["check_results"][7], "check_code": "CF-007"}
            ),
            "RISK_CHECK_DUPLICATED",
        ),
        (
            lambda body: body["check_results"].__setitem__(
                7, {**body["check_results"][7], "check_code": "XX-999"}
            ),
            "RISK_DIRECT_SCHEMA_INVALID",
        ),
        (
            lambda body: body["check_results"][0]["findings"][0].update(
                {"check_code": "CF-002"}
            ),
            "RISK_FINDING_CHECK_INVALID",
        ),
        (
            lambda body: body["check_results"][0]["findings"][0]["evidence"][0].update(
                {"ir_ref": "I999"}
            ),
            "RISK_EVIDENCE_IR_UNKNOWN",
        ),
        (
            lambda body: body["check_results"][0]["findings"][0]["evidence"][0].update(
                {"evidence_ref": "A999"}
            ),
            "RISK_EVIDENCE_ANCHOR_UNKNOWN",
        ),
    ],
)
def test_invalid_direct_output_never_returns_partial_success(mutate, expected_code) -> None:
    payload = _valid_payload()
    mutate(payload)
    responses = [
        _completion(json.dumps(payload, ensure_ascii=False)),
        _completion(json.dumps(payload, ensure_ascii=False), repair_no=1),
    ]

    with pytest.raises(DirectReviewError) as raised:
        _review(responses)

    assert raised.value.code == expected_code


def test_failed_commercial_check_returns_partial_result() -> None:
    payload = _valid_payload(finding_count=0)
    payload["check_results"][0].update(
        {
            "status": "FAILED",
            "decision_note": "当前检查无法形成可靠结论。",
        }
    )

    result, runtime = _review(
        [_completion(json.dumps(payload, ensure_ascii=False))]
    )

    assert result.status == "PARTIAL_FAILED"
    assert result.check_results[0].status == "FAILED"
    assert result.check_results[0].reason_code == "CHECK_FAILED"
    assert len(result.check_results) == 8
    assert len(runtime.calls) == 1


def test_missing_model_reason_code_is_enriched_without_repair() -> None:
    payload = _valid_payload()
    for check in payload["check_results"]:
        del check["reason_code"]

    result, runtime = _review(
        [_completion(json.dumps(payload, ensure_ascii=False))]
    )

    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.reason_code_enrichment_count == 8
    assert result.ignored_model_reason_code_count == 0
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("model_value", [None, "", "MODEL_INVENTED_VALUE"])
def test_model_reason_code_is_ignored_and_deterministically_overwritten(
    model_value,
) -> None:
    payload = _valid_payload()
    for check in payload["check_results"]:
        check["reason_code"] = model_value

    parsed = _parse_model_output(json.dumps(payload, ensure_ascii=False))
    enriched, metrics = _enrich_reason_codes(parsed.response)
    by_code = {item.check_code: item for item in enriched.check_results}

    assert by_code["CF-001"].reason_code == "RISK_IDENTIFIED"
    assert by_code["CF-002"].reason_code == "NO_RISK_IDENTIFIED"
    assert by_code["CF-005"].reason_code == "NO_RISK_IDENTIFIED"
    assert metrics.enrichment_count == 8
    assert metrics.ignored_model_reason_code_count == 8
    assert metrics.rule_version == "1.0"


def test_reason_code_mapping_for_not_applicable_failed_and_insufficient() -> None:
    not_applicable = _valid_payload(finding_count=0)
    not_applicable["check_results"][0]["status"] = "NOT_APPLICABLE"
    failed = _valid_payload(finding_count=0)
    failed["check_results"][0]["status"] = "FAILED"
    insufficient = _valid_payload(finding_count=0)
    cf005 = insufficient["check_results"][4]
    cf005.update(
        {
            "status": "FAILED",
            "candidate_decision": "INSUFFICIENT_EVIDENCE",
            "identified_security_mechanisms": [],
            "candidate_evidence": [],
        }
    )

    not_applicable_final = _enrich_reason_codes(
        _parse_model_output(json.dumps(not_applicable, ensure_ascii=False)).response
    )[0]
    failed_final = _enrich_reason_codes(
        _parse_model_output(json.dumps(failed, ensure_ascii=False)).response
    )[0]
    insufficient_final = _enrich_reason_codes(
        _parse_model_output(json.dumps(insufficient, ensure_ascii=False)).response
    )[0]

    assert not_applicable_final.check_results[0].reason_code == "NOT_APPLICABLE"
    assert failed_final.check_results[0].reason_code == "CHECK_FAILED"
    assert insufficient_final.check_results[4].reason_code == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("invalid_reason", [None, ""])
def test_final_check_result_rejects_null_or_empty_reason_code(invalid_reason) -> None:
    with pytest.raises(ValidationError):
        ModelCheckCoverageResult.model_validate(
            {
                "check_code": "CF-001",
                "status": "REVIEWED",
                "reason_code": invalid_reason,
                "decision_note": "已检查合同价款口径，未发现对我方不利的实质风险。",
                "findings": [],
            }
        )


def test_reason_code_enrichment_does_not_change_findings_risk_level_or_evidence() -> None:
    payload = _valid_payload()
    for check in payload["check_results"]:
        check["reason_code"] = None
    parsed = _parse_model_output(json.dumps(payload, ensure_ascii=False))
    before = [
        item.model_dump(mode="json") for item in parsed.response.check_results[0].findings
    ]

    enriched, _metrics = _enrich_reason_codes(parsed.response)
    after = [
        item.model_dump(mode="json") for item in enriched.check_results[0].findings
    ]

    assert after == before
    assert after[0]["risk_level"] == "HIGH"
    assert after[0]["evidence"] == before[0]["evidence"]


def test_known_top_level_checks_is_normalized_without_model_repair() -> None:
    payload = _valid_payload()
    payload["checks"] = payload.pop("check_results")

    result, runtime = _review([_completion(json.dumps(payload, ensure_ascii=False))])

    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.schema_normalization_applied is True
    assert result.schema_normalization_type == "TOP_LEVEL_CHECKS_TO_CHECK_RESULTS"
    assert result.attempt_diagnostics[0].schema_normalization_applied is True
    assert len(runtime.calls) == 1


def test_extra_top_level_explanation_is_discarded_without_model_repair() -> None:
    payload = _valid_payload()
    payload["summary"] = "已完成八项检查。"

    result, runtime = _review([_completion(json.dumps(payload, ensure_ascii=False))])

    assert result.model_call_count == 1
    assert result.repair_count == 0
    assert result.schema_normalization_applied is True
    assert len(result.check_results) == 8
    assert len(runtime.calls) == 1


def test_cf005_ir_ref_shorthand_is_bound_to_prompt_anchor_without_repair() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, _candidate = _prompt(request)
    payload = _valid_payload()
    payload["check_results"][4]["candidate_evidence"] = ["I001"]

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
    )

    assert parsed.normalization.normalization_type == (
        "CF005_EVIDENCE_REFS_TO_DRAFTS"
    )
    assert parsed.raw_object["check_results"][4]["candidate_evidence"] == [
        "I001"
    ]
    assert parsed.response.check_results[4].candidate_evidence[0].model_dump(
        mode="json"
    ) == {
        "evidence_type": "TEXT_QUOTE",
        "ir_ref": "I001",
        "evidence_ref": "A001",
        "checked_scope": None,
        "verification_note": None,
    }


def test_cf005_unknown_ir_ref_shorthand_is_not_normalized() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, _candidate = _prompt(request)
    payload = _valid_payload()
    payload["check_results"][4]["candidate_evidence"] = ["I999"]

    with pytest.raises(DirectReviewError) as raised:
        _parse_model_output(
            json.dumps(payload, ensure_ascii=False),
            ir_refs=ir_refs,
            anchor_refs=anchor_refs,
        )

    assert raised.value.code == "RISK_DIRECT_SCHEMA_INVALID"


def test_cf005_anchor_ref_shorthand_is_uniquely_bound_to_ir() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, _candidate = _prompt(request)
    payload = _valid_payload()
    payload["check_results"][4]["candidate_evidence"] = [
        {"evidence_ref": "A001"}
    ]

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
    )

    assert parsed.normalization.normalization_type == (
        "CF005_EVIDENCE_REFS_TO_DRAFTS"
    )
    assert parsed.response.check_results[4].candidate_evidence[0].ir_ref == "I001"
    assert parsed.response.check_results[4].candidate_evidence[0].evidence_ref == (
        "A001"
    )


def test_empty_decision_notes_require_explanation_repair_not_fake_no_risk() -> None:
    payload = _valid_payload()
    for check in payload["check_results"]:
        check["decision_note"] = ""
    with pytest.raises(DirectReviewError) as error:
        _parse_model_output(json.dumps(payload, ensure_ascii=False))
    assert error.value.check_codes == list(COMMERCIAL_CHECK_CODES)
    assert all(check["decision_note"] == "" for check in error.value.normalized_output["check_results"])


def test_null_notes_do_not_hide_successful_reference_normalization() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, _candidate = _prompt(request)
    payload = _valid_payload()
    for check in payload["check_results"]:
        check["decision_note"] = None
    payload["check_results"][4]["candidate_evidence"] = ["A001"]
    with pytest.raises(DirectReviewError) as error:
        _parse_model_output(json.dumps(payload, ensure_ascii=False), ir_refs=ir_refs, anchor_refs=anchor_refs)
    normalized = error.value.normalized_output
    assert normalized["check_results"][4]["candidate_evidence"][0]["evidence_ref"] == "A001"
    assert normalized["check_results"][0]["decision_note"] is None


def test_cf005_reference_is_normalized_but_missing_note_remains_explicit() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, _candidate = _prompt(request)
    payload = _valid_payload()
    cf005 = payload["check_results"][4]
    cf005.pop("decision_note")
    cf005["candidate_evidence"] = "A001条款已提供明确履约保障。"
    with pytest.raises(DirectReviewError) as error:
        _parse_model_output(json.dumps(payload, ensure_ascii=False), ir_refs=ir_refs, anchor_refs=anchor_refs)
    assert error.value.check_codes == ["CF-005"]
    assert error.value.normalized_output["check_results"][4]["candidate_evidence"][0]["evidence_ref"] == "A001"


def test_cf005_prose_evidence_uses_only_deterministic_mechanism_anchors() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    assert candidate.identified_security_mechanisms == ["ACCEPTANCE_LINKAGE"]
    payload = _valid_payload()
    payload["check_results"][4]["candidate_evidence"] = (
        "合同约定的付款安排已经提供履约保障。"
    )

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
    )

    assert [
        item.evidence_ref
        for item in parsed.response.check_results[4].candidate_evidence
    ] == ["A001", "A002", "A003", "A004", "A005"]


def test_single_finding_evidence_object_is_wrapped_without_model_repair() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    payload["check_results"][0]["findings"][0]["evidence"] = payload[
        "check_results"
    ][0]["findings"][0]["evidence"][0]

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
    )

    assert len(parsed.response.check_results[0].findings[0].evidence) == 1


def test_single_finding_object_and_evidence_object_are_wrapped() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    finding = payload["check_results"][0]["findings"][0]
    finding["evidence"] = finding["evidence"][0]
    payload["check_results"][0]["findings"] = finding

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    assert len(parsed.response.check_results[0].findings) == 1
    assert len(parsed.response.check_results[0].findings[0].evidence) == 1


def test_check_level_classification_is_moved_to_missing_finding_fields() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    first = payload["check_results"][0]
    first["category"] = first["findings"][0].pop("category")
    first["risk_type"] = first["findings"][0].pop("risk_type")

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    finding = parsed.response.check_results[0].findings[0]
    assert finding.category == "PAYMENT"
    assert finding.risk_type == "PRICE_CALCULATION_RISK"


def test_allowed_nonprimary_check_risk_type_is_preserved_on_finding() -> None:
    request = _request()
    specs = list(request.assigned_check_specs)
    specs[0] = specs[0].model_copy(
        update={
            "allowed_risk_types": [
                "PRICE_CALCULATION_RISK",
                "PRICE_SCOPE_RISK",
            ]
        }
    )
    request = request.model_copy(update={"assigned_check_specs": specs})
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    first = payload["check_results"][0]
    first["category"] = first["findings"][0].pop("category")
    first["risk_type"] = "PRICE_SCOPE_RISK"
    first["findings"][0].pop("risk_type")

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    finding = parsed.response.check_results[0].findings[0]
    assert finding.category == "PAYMENT"
    assert finding.risk_type == "PRICE_SCOPE_RISK"


def test_cf005_missing_candidate_evidence_uses_deterministic_mechanism_sources() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    payload["check_results"][4]["candidate_evidence"] = []

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    assert [
        item.evidence_ref
        for item in parsed.response.check_results[4].candidate_evidence
    ] == ["A001", "A002", "A003", "A004", "A005"]


def test_cf005_confirmed_risk_defaults_missing_candidate_detail_lists() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    cf005 = payload["check_results"][4]
    cf005["candidate_decision"] = "RISK_CONFIRMED"
    cf005["findings"] = [
        {
            **payload["check_results"][0]["findings"][0],
            "check_code": "CF-005",
            "risk_type": "ADVANCE_PAYMENT_SECURITY_RISK",
        }
    ]
    cf005.pop("identified_security_mechanisms")
    cf005.pop("candidate_evidence")

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    normalized = parsed.response.check_results[4]
    assert normalized.identified_security_mechanisms == []
    assert normalized.candidate_evidence == []


def test_cf005_single_candidate_evidence_object_is_wrapped() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload()
    cf005 = payload["check_results"][4]
    cf005["candidate_evidence"] = cf005["candidate_evidence"][0]

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    assert len(parsed.response.check_results[4].candidate_evidence) == 1


def test_cf005_missing_decision_is_rejected_by_deterministic_payment_facts() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    assert candidate.identified_security_mechanisms
    payload = _valid_payload()
    payload["check_results"][4].pop("candidate_decision")

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
        cf005_candidate=candidate,
        assigned_check_specs=request.assigned_check_specs,
    )

    assert (
        parsed.response.check_results[4].candidate_decision
        == "TRIGGER_NOT_MET"
    )


def test_cf005_missing_strong_decision_does_not_manufacture_a_finding() -> None:
    request = _request_with_single_payment_text("本合同签订后十日内，甲方应一次性支付全部合同价款。")
    _text, ir_refs, anchor_refs, candidate = _prompt(request)
    payload = _valid_payload(finding_count=0)
    for key in ("candidate_decision", "identified_security_mechanisms", "candidate_evidence"):
        payload["check_results"][4].pop(key)
    with pytest.raises(DirectReviewError) as error:
        _parse_model_output(json.dumps(payload, ensure_ascii=False), ir_refs=ir_refs,
                            anchor_refs=anchor_refs, cf005_candidate=candidate,
                            assigned_check_specs=request.assigned_check_specs)
    assert error.value.check_codes == ["CF-005"]
    assert error.value.normalized_output["check_results"][4]["findings"] == []


def test_missing_text_evidence_type_is_restored_from_exact_prompt_binding() -> None:
    request = _request()
    _text, ir_refs, anchor_refs, _candidate = _prompt(request)
    payload = _valid_payload()
    evidence = payload["check_results"][0]["findings"][0]["evidence"][0]
    del evidence["evidence_type"]

    parsed = _parse_model_output(
        json.dumps(payload, ensure_ascii=False),
        ir_refs=ir_refs,
        anchor_refs=anchor_refs,
    )

    assert parsed.normalization.normalization_type == (
        "CF005_EVIDENCE_REFS_TO_DRAFTS"
    )
    assert parsed.response.check_results[0].findings[0].evidence[0].evidence_type == (
        "TEXT_QUOTE"
    )


@pytest.mark.parametrize(
    "payload",
    [
        lambda: {
            **_valid_payload(),
            "checks": _valid_payload()["check_results"],
        },
        lambda: {
            "checks": _valid_payload()["check_results"],
            "unknown": True,
        },
        lambda: {
            "checks": [
                *(_valid_payload()["check_results"][:-1]),
                {
                    **_valid_payload()["check_results"][-1],
                    "check_code": "XX-999",
                },
            ]
        },
        lambda: {
            "checks": [
                *(_valid_payload()["check_results"][:-1]),
                {
                    **_valid_payload()["check_results"][-1],
                    "check_code": "CF-007",
                },
            ]
        },
    ],
)
def test_unknown_or_ambiguous_schema_is_never_normalized(payload) -> None:
    with pytest.raises(DirectReviewError) as raised:
        _parse_model_output(json.dumps(payload(), ensure_ascii=False))

    assert raised.value.code == "RISK_DIRECT_SCHEMA_INVALID"


def test_schema_normalization_preserves_findings_and_evidence_exactly() -> None:
    payload = _valid_payload()
    expected = json.loads(json.dumps(payload["check_results"], ensure_ascii=False))
    payload["checks"] = payload.pop("check_results")

    parsed = _parse_model_output(json.dumps(payload, ensure_ascii=False))

    assert (
        parsed.response.model_dump(mode="json", exclude_unset=True)["check_results"]
        == expected
    )
    assert parsed.raw_object["checks"] == expected


def test_candidate_ir_is_not_an_allowed_output_field() -> None:
    payload = _valid_payload()
    for check in payload["check_results"]:
        check["candidate_ir"] = []

    with pytest.raises(DirectReviewError) as raised:
        _parse_model_output(json.dumps(payload, ensure_ascii=False))

    assert raised.value.code == "RISK_DIRECT_SCHEMA_INVALID"


def test_repair_may_remove_only_candidate_ir_without_changing_decisions() -> None:
    first = _valid_payload()
    for check in first["check_results"]:
        check["candidate_ir"] = []
    repaired = _valid_payload()

    result, _runtime = _review(
        [
            _completion(json.dumps(first, ensure_ascii=False)),
            _completion(json.dumps(repaired, ensure_ascii=False), repair_no=1),
        ]
    )

    assert result.repair_count == 1
    assert result.attempt_diagnostics[-1].semantic_preservation_passed is True
    assert len(result.findings) == 1


def test_repair_cannot_empty_nonempty_findings() -> None:
    first = _valid_payload()
    first["unexpected_top_level"] = True
    repaired = _valid_payload(finding_count=0)

    with pytest.raises(DirectReviewError) as raised:
        _review(
            [
                _completion(json.dumps(first, ensure_ascii=False)),
                _completion(json.dumps(repaired, ensure_ascii=False), repair_no=1),
            ]
        )

    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["check_results"][0]["findings"].pop(),
        lambda body: body["check_results"][0]["findings"][0].update(
            {"check_code": "CF-002"}
        ),
        lambda body: body["check_results"][1]["findings"].append(
            json.loads(json.dumps(body["check_results"][0]["findings"][0]))
        ),
        lambda body: body["check_results"][0].update({"status": "NOT_APPLICABLE"}),
    ],
)
def test_repair_semantic_preservation_gate_rejects_business_changes(mutate) -> None:
    first = _valid_payload()
    first["unexpected_top_level"] = True
    repaired = _valid_payload()
    mutate(repaired)

    with pytest.raises(DirectReviewError) as raised:
        _review(
            [
                _completion(json.dumps(first, ensure_ascii=False)),
                _completion(json.dumps(repaired, ensure_ascii=False), repair_no=1),
            ]
        )

    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"


def test_repair_cannot_delete_one_of_multiple_valid_evidence_items() -> None:
    first = _valid_payload()
    original_evidence = first["check_results"][0]["findings"][0]["evidence"][0]
    second_evidence = {
        **json.loads(json.dumps(original_evidence)),
        "ir_ref": "I002",
        "evidence_ref": "A002",
    }
    first["check_results"][0]["findings"][0]["evidence"].append(second_evidence)
    first["unexpected_top_level"] = True
    repaired = _valid_payload()

    with pytest.raises(DirectReviewError) as raised:
        _review(
            [
                _completion(json.dumps(first, ensure_ascii=False)),
                _completion(json.dumps(repaired, ensure_ascii=False), repair_no=1),
            ]
        )

    assert raised.value.code == "RISK_REPAIR_SEMANTICS_CHANGED"


def test_repair_may_only_remove_unknown_schema_field() -> None:
    first = _valid_payload()
    first["unexpected_top_level"] = True
    repaired = _valid_payload()

    result, _runtime = _review(
        [
            _completion(json.dumps(first, ensure_ascii=False)),
            _completion(json.dumps(repaired, ensure_ascii=False), repair_no=1),
        ]
    )

    assert result.repair_count == 1
    assert result.attempt_diagnostics[-1].semantic_preservation_passed is True
    assert len(result.findings) == 1


def test_cf003_cf004_cf005_decision_policies_are_explicit_and_bounded() -> None:
    assert set(COMMERCIAL_DECISION_POLICIES) == {"CF-003", "CF-004", "CF-005"}
    for code, policy in COMMERCIAL_DECISION_POLICIES.items():
        assert policy.check_code == code
        assert policy.review_object
        assert len(policy.triggers) >= 2
        assert len(policy.non_risk_examples) >= 2
        assert policy.minimum_evidence
        assert policy.category == "PAYMENT"
        assert policy.risk_level_rule
        assert policy.boundary


def test_cf004_requires_source_text_not_absence_only() -> None:
    payload = _valid_payload(finding_count=0)
    check = payload["check_results"][3]
    check["findings"] = [
        {
            "check_code": "CF-004",
            "category": "PAYMENT",
            "risk_type": "PAYMENT_ADJUSTMENT_RISK",
            "risk_level": "MEDIUM",
            "title": "调整机制不完整",
            "issue": "费用调整后果未写明",
            "impact_to_our_party": "我方扣款权可能无法执行",
            "suggestion": "明确触发条件、公式和法律后果",
            "evidence": [
                {
                    "evidence_type": "ABSENCE",
                    "ir_ref": None,
                    "evidence_ref": None,
                    "checked_scope": "费用调整条款",
                    "verification_note": "未发现完整调整公式",
                }
            ],
        }
    ]
    responses = [
        _completion(json.dumps(payload, ensure_ascii=False)),
        _completion(json.dumps(payload, ensure_ascii=False), repair_no=1),
    ]

    with pytest.raises(DirectReviewError) as raised:
        _review(responses)

    assert raised.value.code == "RISK_CF004_EVIDENCE_INVALID"


def test_cf005_requires_payment_quote_and_absence_of_safeguard() -> None:
    payload = _valid_payload(finding_count=0)
    check = payload["check_results"][4]
    check["candidate_decision"] = "RISK_CONFIRMED"
    check["identified_security_mechanisms"] = []
    check["candidate_evidence"] = []
    check["findings"] = [
        {
            "check_code": "CF-005",
            "category": "PAYMENT",
            "risk_type": "ADVANCE_PAYMENT_SECURITY_RISK",
            "risk_level": "HIGH",
            "title": "预付款缺少履约保障",
            "issue": "主要履约前支付全部价款且无保障",
            "impact_to_our_party": "付款后缺少履约及返还保障",
            "suggestion": "改为分期付款或增加履约保函",
            "evidence": [
                {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": "I001",
                    "evidence_ref": "A001",
                    "checked_scope": None,
                    "verification_note": None,
                },
                {
                    "evidence_type": "ABSENCE",
                    "ir_ref": None,
                    "evidence_ref": None,
                    "checked_scope": "履约保障及退款返还条款",
                    "verification_note": "未发现履约保函、保证金、分期、退款或担保",
                },
            ],
        }
    ]

    result, _runtime = _review(
        [_completion(json.dumps(payload, ensure_ascii=False))],
        _request_with_single_payment_text(
            "本合同签订后十日内，甲方应一次性支付全部合同价款。"
        ),
    )

    finding = next(item for item in result.findings if item.check_code == "CF-005")
    assert {item.evidence_type for item in finding.evidence_candidates} == {
        "TEXT_QUOTE",
        "ABSENCE",
    }


def test_cf005_enriches_confirmed_advance_payment_with_deterministic_absence() -> None:
    payload = _valid_payload(finding_count=0)
    check = payload["check_results"][4]
    check["candidate_decision"] = "RISK_CONFIRMED"
    check["identified_security_mechanisms"] = []
    check["candidate_evidence"] = []
    check["findings"] = [
        {
            "check_code": "CF-005",
            "category": "PAYMENT",
            "risk_type": "ADVANCE_PAYMENT_SECURITY_RISK",
            "risk_level": "HIGH",
            "title": "预付款缺少履约保障",
            "issue": "主要履约前支付全部价款",
            "impact_to_our_party": "付款后缺少履约保障",
            "suggestion": "增加履约保函",
            "evidence": [
                {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": "I001",
                    "evidence_ref": "A001",
                    "checked_scope": None,
                    "verification_note": None,
                }
            ],
        }
    ]
    result, _runtime = _review(
        [_completion(json.dumps(payload, ensure_ascii=False))],
        _request_with_single_payment_text(
            "本合同签订后十日内，甲方应一次性支付全部合同价款。"
        ),
    )

    finding = next(item for item in result.findings if item.check_code == "CF-005")
    assert {item.evidence_type for item in finding.evidence_candidates} == {
        "TEXT_QUOTE",
        "ABSENCE",
    }
    absence = next(
        item for item in finding.evidence_candidates if item.evidence_type == "ABSENCE"
    )
    assert "Python确定性候选扫描" in (absence.verification_note or "")


def _request_with_single_payment_text(text: str) -> CommercialReviewRequest:
    payload = _request().model_dump(mode="json")
    payload["projected_ir_items"] = [
        {
            "ir_type": "payment_terms",
            "item_id": "ir-payment-special",
            "subject": "甲方",
            "predicate": "支付",
            "object": text,
            "source_anchors": [{"anchor_id": "anchor-payment-special"}],
        }
    ]
    payload["source_excerpts"] = [
        {
            "anchor_id": "anchor-payment-special",
            "block_id": "block-payment-special",
            "block_no": 1,
            "page_number": None,
            "char_start": 0,
            "char_end": len(text),
            "quoted_text": text,
            "quoted_text_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "heading_path": ["付款"],
        }
    ]
    return CommercialReviewRequest.model_validate(payload)


def test_cf005_candidate_detects_full_prepayment_without_security() -> None:
    request = _request_with_single_payment_text(
        "本合同签订后十日内，甲方应一次性支付全部合同价款。"
    )

    text, _ir_refs, _anchor_refs, candidate = _prompt(request)
    payload = json.loads(text.split("\n", 1)[1])

    assert candidate.substantial_prepayment is True
    assert candidate.payment_before_performance is True
    assert candidate.identified_security_mechanisms == []
    assert candidate.candidate_ir_refs == ["I001"]
    assert candidate.candidate_evidence_refs == ["A001"]
    assert candidate.payer_role_status == "OUR_PARTY"
    assert payload["review_context"]["party_a"] == request.our_party
    assert payload["review_context"]["party_b"] == request.counterparty
    assert payload["review_context"]["our_contract_role"] == "甲方"


def test_cf005_candidate_detects_installment_and_acceptance_linkage() -> None:
    request = _request_with_single_payment_text(
        "合同价款分期支付，首期30%，尾款70%在交付并验收合格后支付。"
    )

    candidate = _prompt(request)[3]

    assert candidate.installment_payment is True
    assert candidate.acceptance_linked is True
    assert "INSTALLMENT_PAYMENT" in candidate.identified_security_mechanisms
    assert "ACCEPTANCE_LINKAGE" in candidate.identified_security_mechanisms


def test_cf005_candidate_detects_guarantee_or_deposit() -> None:
    request = _request_with_single_payment_text(
        "甲方在合同生效后支付全部价款，乙方同时提交履约保函和履约保证金。"
    )

    candidate = _prompt(request)[3]

    assert candidate.substantial_prepayment is True
    assert candidate.payment_before_performance is True
    assert set(candidate.identified_security_mechanisms) >= {
        "PERFORMANCE_GUARANTEE",
        "PERFORMANCE_DEPOSIT",
    }
    assert candidate.candidate_evidence_refs == ["A001"]


def test_cf005_strong_candidate_cannot_be_skipped_by_model() -> None:
    request = _request_with_single_payment_text(
        "本合同签订后十日内，甲方应一次性支付全部合同价款。"
    )
    payload = _valid_payload(finding_count=0)
    cf005 = payload["check_results"][4]
    cf005["candidate_decision"] = "RISK_NOT_CONFIRMED"
    cf005["identified_security_mechanisms"] = ["OTHER_SECURITY"]
    cf005["candidate_evidence"] = [
        {
            "evidence_type": "TEXT_QUOTE",
            "ir_ref": "I001",
            "evidence_ref": "A001",
            "checked_scope": None,
            "verification_note": None,
        }
    ]
    runtime = FakeRuntime(
        [
            _completion(json.dumps(payload, ensure_ascii=False)),
            _completion(json.dumps(payload, ensure_ascii=False), repair_no=1),
        ]
    )
    reviewer = CommercialFinancialDirectReviewer(runtime_factory=lambda _tenant: runtime)

    with pytest.raises(DirectReviewError) as raised:
        asyncio.run(
            reviewer.review(
                request,
                tenant_id="tenant-1",
                model_id="deepseek-v4-flash",
                framework_run_id="run-1",
            )
        )

    assert raised.value.code == "RISK_CF005_CANDIDATE_SKIPPED"
