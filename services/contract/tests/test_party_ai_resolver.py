import asyncio
import json
from types import SimpleNamespace

import pytest

from contract.party.ai_resolver import PartyAnswer, VERSION, bind_answer, resolve_parties_ai, select_source_pack
from services.contract.capabilities.party_ai import rule_role_arguments


TEXT = "技术服务合同\n服务方（甲方）：晨星公司；委托方（乙方）：海川公司。"


def block(text=TEXT, i=1, prefix="block"):
    return SimpleNamespace(block_id=f"{prefix}-{i}", block_no=i, text=text,
                           page_number=i, char_start=200, char_end=200 + len(text))


def answer():
    return {"party_a": {"status": "RESOLVED", "name": "晨星公司", "business_roles": ["服务方"],
                        "sources": [{"ref": "F001", "quote": "服务方（甲方）：晨星公司"}]},
            "party_b": {"status": "RESOLVED", "name": "海川公司", "business_roles": ["委托方"],
                        "sources": [{"ref": "F001", "quote": "委托方（乙方）：海川公司"}]}}


class Runtime:
    def __init__(self, response=None, delay=0):
        self.calls = 0
        self.response = response or answer()
        self.delay = delay

    async def complete_with_usage(self, **kwargs):
        self.calls += 1
        assert kwargs["thinking_override"] is False
        assert kwargs["max_tokens"] == 1000
        assert kwargs["review_unit_id"] == "party_identification"
        assert "tools" not in kwargs
        payload = json.loads(kwargs["messages"][0]["content"])
        assert "char_start" not in payload["fragments"][0]
        assert "page_number" not in payload["fragments"][0]
        await asyncio.sleep(self.delay)
        return SimpleNamespace(content=json.dumps(self.response, ensure_ascii=False),
                               prompt_tokens=300, completion_tokens=120)


async def resolve(tmp_path, runtime, blocks=None, **kwargs):
    return await resolve_parties_ai(blocks or [block()], runtime_factory=lambda: runtime,
        tenant_id=kwargs.pop("tenant_id", "tenant-1"), model_id="fake", review_id="review-1", run_id="run-1",
        cache_directory=tmp_path, **kwargs)


def test_source_pack_retains_declaration_neighbours_and_signature_without_sending_whole_contract():
    blocks = [block("技术服务合同", 1), *[block("普通履约条款。" * 20, i) for i in range(2, 80)],
              block("服务方（甲方）：", 80), block("晨星公司", 81),
              block("委托方（乙方）：海川公司", 82), block("双方签章：", 83)]
    pack = select_source_pack(blocks)
    ids = {f.block_id for f in pack.fragments}
    assert {"block-1", "block-80", "block-81", "block-82", "block-83"} <= ids
    assert sum(len(f.text) for f in pack.fragments) <= 6000
    assert pack.omitted_chars > 0


def test_long_block_exact_local_offsets_and_no_page_number_invention():
    text = "无关正文。" * 1700 + TEXT + "\n签章"
    source = block(text)
    source.page_number = None
    pack = select_source_pack([source])
    fragment = next(f for f in pack.fragments if TEXT in f.text)
    raw = answer()
    for party in raw.values():
        party["sources"][0]["ref"] = fragment.ref
    result = bind_answer(PartyAnswer.model_validate(raw), pack)
    anchor = result["party_a"]["anchors"][0]
    assert text[anchor["char_start"]:anchor["char_end"]] == "服务方（甲方）：晨星公司"
    assert anchor["page_number"] is None
    assert anchor["char_start"] == text.index("服务方（甲方）")


def test_service_provider_can_be_a_and_selected_b_remains_client(tmp_path):
    runtime = Runtime()
    result = asyncio.run(resolve(tmp_path, runtime))
    assert result["parties"]["party_a"]["name"] == "晨星公司"
    assert result["parties"]["party_a"]["business_roles"] == ["服务方"]
    assert rule_role_arguments(result, "PARTY_B") == {"business_role": "委托方", "business_roles": ["委托方"], "infer_business_role": False}
    assert result["model_call_count"] == runtime.calls == 1


@pytest.mark.parametrize("kind", ["invented_name", "invented_quote", "invalid_ref", "no_quote", "ambiguous"])
def test_source_mismatch_does_not_reject_ai_identity_or_invent_coordinates(kind):
    raw = answer()
    a = raw["party_a"]
    text = TEXT
    if kind == "invented_name":
        a["name"] = "不存在公司"
    elif kind == "invented_quote":
        a["sources"][0]["quote"] = "原文不存在"
    elif kind == "invalid_ref":
        a["sources"][0]["ref"] = "F999"
    elif kind == "no_quote":
        a["sources"] = []
    else:
        text += TEXT
    result = bind_answer(PartyAnswer.model_validate(raw), select_source_pack([block(text)]))
    assert result["party_a"]["name"] == a["name"]
    assert result["party_a"]["business_roles"] == ["服务方"]
    assert result["party_a"]["identity_source_validation"] == "NOT_PERFORMED"
    if kind != "invented_name":
        assert result["party_a"]["anchors"] == []


@pytest.mark.parametrize("kind", ["whitespace", "invalid_ref", "no_quote", "name_without_quote_match"])
def test_unmatched_model_response_is_accepted_and_cached_without_extra_call(tmp_path, kind):
    raw = answer()
    party = raw["party_a"]
    if kind == "whitespace":
        party["sources"][0]["quote"] = "服务方（甲方）： 晨星公司"
    elif kind == "invalid_ref":
        party["sources"][0]["ref"] = "F999"
    elif kind == "no_quote":
        party.pop("sources")
    else:
        party["name"] = "晨星有限公司"
    runtime = Runtime(raw)
    first = asyncio.run(resolve(tmp_path, runtime))
    second = asyncio.run(resolve(tmp_path, runtime))
    assert first["parties"]["party_a"]["name"] == party["name"]
    assert first["parties"]["party_b"]["name"] == "海川公司"
    assert first["parties"] == second["parties"]
    assert runtime.calls == 1 and second["cache_hit"]
    if kind != "name_without_quote_match":
        assert first["parties"]["party_a"]["anchors"] == []


def test_basic_response_schema_is_still_required():
    from pydantic import ValidationError
    raw = answer()
    raw["party_a"]["status"] = "INVALID"
    with pytest.raises(ValidationError):
        PartyAnswer.model_validate(raw)


def test_conflicts_and_multiple_roles_are_not_arbitrarily_assigned():
    raw = answer()
    raw["party_a"]["status"] = "CONFLICT"
    bound = bind_answer(PartyAnswer.model_validate(raw), select_source_pack([block()]))
    assert bound["party_a"]["name"] is None and not bound["party_a"]["business_roles"]
    metadata = {"party_resolution_engine": VERSION, "parties": bound}
    assert rule_role_arguments(metadata, "PARTY_A")["business_role"] is None
    bound["party_b"]["business_roles"] = ["委托方", "采购方"]
    assert rule_role_arguments(metadata, "PARTY_B")["business_role"] is None
    assert rule_role_arguments(metadata, "PARTY_B")["business_roles"] == ["委托方", "采购方"]
    assert rule_role_arguments({}, "PARTY_B") == {}


def test_cache_rebinds_current_coordinates_and_does_not_charge_again(tmp_path):
    runtime = Runtime()
    first = asyncio.run(resolve(tmp_path, runtime))
    second = asyncio.run(resolve(tmp_path, runtime, [block(prefix="new-parse")]))
    assert runtime.calls == 1
    assert second["model_call_count"] == 0 and second["cache_hit"]
    assert second["parties"]["party_a"]["anchors"][0]["block_id"] == "new-parse-1"
    assert first["recorded_usage"] == second["recorded_usage"]
    asyncio.run(resolve(tmp_path, runtime, tenant_id="another-tenant"))
    assert runtime.calls == 2
    asyncio.run(resolve(tmp_path, runtime, [block(TEXT + "\n变更条款")]))
    assert runtime.calls == 3


def test_concurrent_preflight_and_review_share_one_model_call(tmp_path):
    runtime = Runtime(delay=0.05)
    async def both():
        return await asyncio.gather(resolve(tmp_path, runtime), resolve(tmp_path, runtime))
    results = asyncio.run(both())
    assert runtime.calls == 1
    assert sorted(r["model_call_count"] for r in results) == [0, 1]


def test_timeout_is_not_automatically_retried(tmp_path):
    runtime = Runtime(delay=0.05)
    for _ in range(2):
        with pytest.raises(ValueError, match="unavailable"):
            asyncio.run(resolve(tmp_path, runtime, timeout_seconds=0.001))
    assert runtime.calls == 1


def test_unknown_business_role_survives_null_stripping_transport(tmp_path):
    from contract.evidence_planning.review_result import RuleReviewResult
    from contract.rule_evidence.execution import RuleLibraryExecution
    from risk_test_data import risk_plan_input
    from test_rule_library_shadow import snapshot
    execution = RuleLibraryExecution(snapshot(tmp_path), tmp_path / "cache")
    result = asyncio.run(execution.run(risk_plan_input(), "42", "fake",
                         contract_type_name="采购合同", business_role=None, infer_business_role=False))
    assert result.status == "SELECTION_UNRESOLVED"
    assert "BUSINESS_ROLE_UNRESOLVED" in result.diagnostics
    assert result.model_calls == 0
    assert RuleReviewResult.model_validate(result.model_dump(exclude_none=True)).business_role is None


def test_a_b_rule_dimension_is_not_lost_when_business_role_is_known():
    from contract.rule_evidence.planner import AdaptiveRuleEvidencePlanner
    from test_rule_evidence_planner import _rule
    from datetime import date
    request = SimpleNamespace(preview_pending=True, review_standard="neutral", tenant_id="42",
        review_as_of_date=date(2026, 9, 6), jurisdiction="CN", perspective="PARTY_B", business_role="服务方")
    rule = _rule("side", name="乙方规则", content="乙方规则", party_stance="乙方").model_copy(update={"rule_type": "general", "jurisdiction": None})
    assert AdaptiveRuleEvidencePlanner._applicable(rule, request)
    assert not AdaptiveRuleEvidencePlanner._applicable(rule.model_copy(update={"party_stance": "甲方"}), request)


@pytest.mark.parametrize("corrected", [False, True])
def test_registered_ai_handler_reuses_preflight_but_keeps_human_confirmation(monkeypatch, tmp_path, corrected):
    from services.contract.capabilities import register as capability
    from service.conversation import llm_runner
    from services.contract.capabilities import party_ai
    from contract.internal.models import ContractBlockData
    runtime = Runtime()
    monkeypatch.setattr(llm_runner, "LlmRuntime", lambda *a, **k: runtime)
    raw_block = ContractBlockData(block_id="block-1", block_no=1, block_type="paragraph",
                                 page_number=1, char_start=0, char_end=len(TEXT), text=TEXT)
    task_input = {"schema_version": "1.0", "attempt_no": 1, "contract_type": "AUTO", "review_attitude": "NEUTRAL",
                  "review_id": "review-1", "business_task_id": "task-1",
                  "contract_version_id": "version-1", "document_id": "document-1", "perspective": "PARTY_A"}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "success": True, "request_id": "req-1", "data": {
                    "review_id": "review-1", "document_id": "document-1", "generation_id": "generation-1",
                    "blocks": [raw_block.model_dump()]}})
    monkeypatch.setattr(party_ai.httpx, "AsyncClient", Client)
    context = SimpleNamespace(task=SimpleNamespace(tenant_id="tenant-1", input_payload_json=task_input),
        run=SimpleNamespace(id="run-1"), artifacts={"parse_contract": SimpleNamespace(content_json={
            "result_type": "PARSE_CONTRACT_STAGE_V1", "document_id": "document-1", "generation_id": "generation-1",
            "block_count": 1, "ir_hash": "sha256:" + "0" * 64})})
    handler = capability._direct_party_resolution_handler("http://unused", "fake", model_id="fake", cache_directory=tmp_path)
    first = asyncio.run(handler(context))
    assert first.output["party_a"]["name"] == "晨星公司"
    task_input.update(perspective="PARTY_B", confirmed_party_a_name="晨星公司",
                      confirmed_party_b_name="人工确认公司" if corrected else "海川公司")
    second = asyncio.run(handler(context))
    assert second.output["our_party"] == task_input["confirmed_party_b_name"]
    assert second.output["party_b"]["name_status"] == "USER_CONFIRMED"
    assert runtime.calls == 1 and second.metadata["cache_hit"]
    assert rule_role_arguments(second.metadata, "PARTY_B")["business_role"] == (None if corrected else "委托方")


def test_ai_flag_registers_separate_bounded_stage_without_changing_legacy(tmp_path):
    from capability_mount import CapabilitySettings
    from test_contract_capability import CapturingRegistry
    from services.contract.capabilities import register as capability
    registry = CapturingRegistry()
    asyncio.run(capability.register(registry, CapabilitySettings({
        "CONTRACT_SERVICE_BASE_URL": "http://unused", "CONTRACT_RESULT_SINK_INTERNAL_TOKEN": "fake",
        "CONTRACT_PARTY_AI_ENABLED": "true", "CONTRACT_PARTY_AI_CACHE_DIR": str(tmp_path)})))
    pipeline = next(p for p in registry.pipelines if p["pipeline_id"] == capability.PARTY_RESOLUTION_PIPELINE_ID)
    assert pipeline["timeout_seconds"] == 90
    assert pipeline['timeout_seconds'] > sum(stage['timeout_seconds'] for stage in pipeline['stages'])
    assert pipeline["stages"][1]["timeout_seconds"] == 18
    assert pipeline["stages"][1]["retry_policy"]["max_attempts"] == 1
