import asyncio
import hashlib
import json
from datetime import date

from contract.legal_evidence.prompting import (review_legal_evidence_catalog,
    selected_legal_evidence_ids, merge_legal_reasoning)
from contract.rule_evidence.execution import RuleLibraryExecution
from contract.rule_evidence.models import RuleEvidencePlanRequest
from contract.rule_evidence.planner import AdaptiveRuleEvidencePlanner
from services.contract.capabilities.party_ai import rule_role_arguments
from contract.party.ai_resolver import VERSION
from risk_test_data import risk_plan_input
from test_rule_evidence_planner import _rule, _issue
from test_rule_library_reviewer import FakeModel
from test_legal_evidence_binding import _bundle


def evidence(check="CF-002"):
    return _bundle("第一条：按期付款。" + "必要上下文；" * 1100 + "但存在例外时另行处理。",
        domain="commercial_financial", check_codes=[check]).evidence[0]


def test_ai_multiple_roles_select_union_without_repeating_rules_or_changing_side(tmp_path):
    roles = rule_role_arguments({"party_resolution_engine": VERSION, "parties": {
        "party_b": {"status": "RESOLVED", "business_roles": ["供货方", "出卖方", "供货方"]}}}, "PARTY_B")
    assert roles["business_roles"] == ["供货方", "出卖方"]
    rules = [_rule(name, name=topic, content=topic + "应有明确约定", status="pending",
        party_stance=stance).model_copy(update={"review_standard": "strong", "contract_type_path": ["采购合同"],
            "rule_type": "dedicated", "jurisdiction": None})
        for name, stance, topic in [("seller", "出卖方", "付款期限"), ("supplier", "供货方", "验收质量"),
            ("common", "双方", "质保维修"), ("buyer", "买受方", "付款期限")]]
    data = b"".join((rule.model_dump_json()+"\n").encode() for rule in rules)
    (tmp_path / "rules.ndjson").write_bytes(data)
    (tmp_path / "manifest.json").write_text(json.dumps({"source_version": "test", "record_count": len(rules),
        "snapshot_hash": "sha256:"+hashlib.sha256(data).hexdigest()}))
    runtime = FakeModel()
    execution = RuleLibraryExecution(tmp_path, tmp_path / "cache", runtime_factory=lambda tenant: runtime)
    value = risk_plan_input()
    value = type(value).model_validate({**value.model_dump(), "perspective": "PARTY_B"})
    async def run():
        return await execution.run(value, "42", "test", contract_type_name="采购合同", standard="strong", **roles)
    result = asyncio.run(run())
    assert result.status == "COMPLETED" and result.perspective == "PARTY_B"
    assert result.business_roles == ["供货方", "出卖方"]
    assert {item.rule_id for item in result.evidence} == {"seller", "supplier", "common"}
    assert len(result.decisions) == len(result.evidence) == 3
    calls = runtime.calls
    assert asyncio.run(run()) == result and runtime.calls == calls


def test_role_union_does_not_bypass_tenant_standard_or_date():
    request = RuleEvidencePlanRequest(review_id="r", generation_id="g", tenant_id="42", contract_type="AUTO",
        perspective="PARTY_B", business_roles=["出卖方", "供货方"], review_standard="strong",
        review_as_of_date=date(2026, 9, 6), source_version="test", issues=[_issue("a", "付款", ["付款"], ["CF-002"])])
    base = _rule("x", name="付款期限", content="明确付款期限", party_stance="供货方").model_copy(update={"review_standard":"strong", "jurisdiction":None})
    assert AdaptiveRuleEvidencePlanner._applicable(base, request)
    for changes in [{"tenant_id":"foreign"}, {"review_standard":"weak"}, {"party_stance":"买受方"},
                    {"effective_from":date(2027,1,1)}]:
        assert not AdaptiveRuleEvidencePlanner._applicable(base.model_copy(update=changes), request)


def test_full_law_body_keeps_end_exception_and_deduplicates():
    item = evidence()
    catalog, size = review_legal_evidence_catalog([item,item])
    assert len(catalog) == 1 and size > 7000
    assert catalog[0]["content_excerpt"] == item.unit.content
    assert catalog[0]["content_truncated"] is False
    assert catalog[0]["citation_label"] == "《测试法规》第1条"
    assert review_legal_evidence_catalog([]) == ([], 0)


def test_citation_requires_explicit_selected_supplied_provision():
    item = evidence()
    valid = "根据《测试法规》第1条，付款期限应当明确。"
    assert selected_legal_evidence_ids([item],[item.evidence_id],prompt_included=True) == [item.evidence_id]
    assert selected_legal_evidence_ids([item],[],prompt_included=False) == []
    enriched = merge_legal_reasoning("合同付款期限不明确。", [valid], [item], "CF-002", prompt_included=True, selected_ids=[item.evidence_id])
    assert valid in enriched and "待核验" in enriched
    assert merge_legal_reasoning("合同付款期限不明确。", [valid], [item], "CF-002", prompt_included=False, selected_ids=[]) == "合同付款期限不明确。"


def test_commercial_and_generic_prompts_never_apply_retired_seven_k_gate():
    from test_contract_risk_direct_review import _request as commercial_request
    from test_contract_risk_base_bundle import _request as generic_request
    from services.contract.capabilities.risk_review import _prompt
    from services.contract.capabilities.risk_review_bundle import _generic_prompt
    for factory, build in [(commercial_request,_prompt),(generic_request,_generic_prompt)]:
        request = factory()
        item = evidence()
        request.legal_evidence = [item]
        prompt = build(request)[0]
        payload = json.loads(prompt.split("\n",1)[1])
        assert payload["legal_evidence_catalog"][0]["content_excerpt"] == item.unit.content
        assert request.legal_evidence_prompt_status == "INCLUDED"
        assert "citation_label" in prompt
        assert build(request)[0] == prompt  # repairs retain full text, no shrinking


def test_actual_finding_binding_requires_selected_id_and_never_uses_unsent_candidates():
    from test_contract_risk_direct_review import _request, _valid_payload
    from services.contract.capabilities.risk_review import _prompt, _finding, ModelFindingDraft
    request = _request()
    item = evidence("CF-001")
    request.legal_evidence = [item]
    _, ir_refs, anchor_refs, _ = _prompt(request)
    raw = _valid_payload()["check_results"][0]["findings"][0]
    raw["issue"] = "根据《测试法规》第1条，应明确计价范围，合同计价口径不清。"
    raw['legal_evidence_ids'] = [item.evidence_id]
    value = ModelFindingDraft.model_validate(raw)
    finding = _finding(request, value, ir_refs, anchor_refs)
    assert finding.legal_evidence_ids == [item.evidence_id]
    assert raw["issue"] in finding.issue and "待核验" in finding.issue
    assert finding.evidence_candidates  # contract sources are retained internally
    assert _finding(request, value.model_copy(update={"issue":"计价口径不清。"}), ir_refs, anchor_refs).legal_evidence_ids == [item.evidence_id]
    assert _finding(request, value.model_copy(update={"legal_evidence_ids":[]}), ir_refs, anchor_refs).legal_evidence_ids == []
    request.legal_evidence_prompt_status = "OMITTED_TOKEN_BUDGET"
    import pytest
    with pytest.raises(RuntimeError, match='UNKNOWN_LEGAL_EVIDENCE_ID'):
        _finding(request, value, ir_refs, anchor_refs)
