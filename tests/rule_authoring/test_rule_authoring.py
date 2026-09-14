import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from backend.rule_authoring import AssistRequest, Draft, RelatedRequest, rank_related, validate_answer
from backend.rule_authoring_api import create_rule_authoring_router
from backend.rule_authoring_form import explicit_change


PATH = ["建设工程合同类", "采购合同", "采购合同"]
TEXT = "我方作为买受方采购设备，中立标准；预付款超过30%应提供银行保函。"


def draft():
    return Draft(name="预付款保函", reviewDirection="预付款", contractTypePath=PATH,
        partyStance="买受方", reviewStandard="neutral", ruleType="dedicated", content=TEXT,
        reviewMethod="核对预付款比例及银行保函约定，无法确定时说明依据不足。")


def answer():
    return dict(reply="已整理，请确认规则卡片。", draft=draft().model_dump(), questions=[], searchTerms=["预付款", "银行保函", "预付"])


def request():
    return AssistRequest(messages=[{"role": "user", "content": TEXT}], contractTypes=[PATH])


def client(monkeypatch, result=None, failure=None):
    import aituge_model.config as config
    monkeypatch.setenv("FRAMEWORK_INTERNAL_TOKEN", "offline-test-token")
    def pack(mode):
        if mode not in {"public", "private"}: raise ValueError("mode")
        return SimpleNamespace(id=mode)
    monkeypatch.setattr(config, "get_model_pack_for_ai_mode", pack)
    calls = []
    class Runtime:
        def __init__(self, **kwargs):
            self.model_runtime_provider = SimpleNamespace(active_pack=SimpleNamespace(llm=SimpleNamespace(id="offline-model")))
            calls.append(kwargs)
        async def complete_with_usage(self, **kwargs):
            calls.append(kwargs)
            if failure: raise failure
            return SimpleNamespace(content=json.dumps(result or answer(), ensure_ascii=False), prompt_tokens=123, completion_tokens=45)
    app = FastAPI()
    app.include_router(create_rule_authoring_router(Runtime))
    return TestClient(app), calls


HEADERS = {"X-Internal-Token": "offline-test-token", "X-Tenant-Id": "7", "X-User-Id": "11", "X-AI-Mode": "private"}


def test_conversation_extracts_only_draft_without_save_or_tools(monkeypatch):
    c, calls = client(monkeypatch)
    response = c.post("/v1/internal/rule-authoring/assist", headers=HEADERS, json=request().model_dump())
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["partyStance"] == "买受方"
    assert not {"id", "status", "source", "tenantId"} & response.json()["draft"].keys()
    assert calls[0] == {"tenant_id": "7", "model_pack_id": "private", "provider_max_retries": 0}
    assert len(calls) == 2 and "tools" not in calls[1]
    assert calls[1]["max_tokens"] == 2500
    assert response.json()["usage"]["promptTokens"] == 123


def test_followup_receives_full_current_card_and_history(monkeypatch):
    c, calls = client(monkeypatch)
    req = request().model_dump()
    req["draft"] = draft().model_dump()
    req["messages"] += [{"role": "assistant", "content": "请确认。"}, {"role": "user", "content": "同时适用于一般设备，请保留30%的要求。"}]
    assert c.post("/v1/internal/rule-authoring/assist", headers=HEADERS, json=req).status_code == 200
    sent = json.loads(calls[1]["messages"][0]["content"])
    assert len(sent["messages"]) == 3 and sent["draft"]["content"] == TEXT


def test_reuses_reimbursement_explicit_edit_without_model(monkeypatch):
    c, calls = client(monkeypatch)
    req = request().model_dump()
    req["draft"] = draft().model_dump()
    req["messages"] = [{"role": "user", "content": "把规则名称改成采购预付款保障"}]
    response = c.post("/v1/internal/rule-authoring/assist", headers=HEADERS, json=req)
    assert response.status_code == 200
    assert response.json()["draft"]["name"] == "采购预付款保障"
    assert response.json()["draft"]["content"] == TEXT
    assert calls == [] and response.json()["usage"]["promptTokens"] == 0
    assert explicit_change(draft(), "把状态改成生效") is None


@pytest.mark.parametrize("change", [
    {"referenceBasis": "公司采购制度第九条"}, {"contractTypePath": ["不存在的分类"]},
    {"status": "active"}, {"reviewStandard": "very_strong"},
    {"effectiveFrom": "2026-02-30"}, {"effectiveFrom": "2027-01-01", "effectiveTo": "2026-01-01"},
])
def test_rejects_invented_basis_or_invalid_fields(monkeypatch, change):
    raw = answer(); raw["draft"].update(change)
    c, calls = client(monkeypatch, raw)
    response = c.post("/v1/internal/rule-authoring/assist", headers=HEADERS, json=request().model_dump())
    assert response.status_code == 502
    assert "draft" not in response.json()
    assert len(calls) == 2  # no paid retry or repair


@pytest.mark.parametrize("headers,status", [({**HEADERS, "X-Internal-Token": "wrong"}, 401),
    ({**HEADERS, "X-Tenant-Id": "null"}, 400), ({**HEADERS, "X-User-Id": " "}, 400)])
def test_requires_trusted_identity_before_any_model(monkeypatch, headers, status):
    c, calls = client(monkeypatch)
    assert c.post("/v1/internal/rule-authoring/assist", headers=headers, json=request().model_dump()).status_code == status
    assert calls == []


def test_timeout_does_not_report_success_or_retry(monkeypatch):
    c, calls = client(monkeypatch, failure=TimeoutError())
    response = c.post("/v1/internal/rule-authoring/assist", headers=HEADERS, json=request().model_dump())
    assert response.status_code == 504 and len(calls) == 2


def test_reference_must_come_from_user_not_previous_assistant():
    req = request().model_copy(update={"messages": [
        *request().messages, __import__('backend.rule_authoring', fromlist=['Message']).Message(role="assistant", content="虚构规定"),
        __import__('backend.rule_authoring', fromlist=['Message']).Message(role="user", content="继续") ]})
    raw = answer(); raw["draft"]["referenceBasis"] = "虚构规定"
    with pytest.raises(ValueError): validate_answer(json.dumps(raw), req)


def test_related_rules_keep_versions_and_warn_on_threshold_and_stance(monkeypatch):
    c, calls = client(monkeypatch)
    candidate = dict(id="41", code="RR-41", version=3, name="预付款保障", content=TEXT.replace("30%", "50%"),
        reviewDirection="预付款", reviewStandard="strong", partyStance="出卖方", contractTypePath=PATH, status="pending")
    response = c.post("/v1/internal/rule-authoring/related", headers=HEADERS, json={"draft": draft().model_dump(),
        "searchTerms": ["预付款", "银行保函"], "candidates": [candidate]})
    assert response.status_code == 200
    item = response.json()["candidates"][0]
    assert item["rule"]["version"] == 3
    assert "数字或比例可能不同" in item["reasons"] and "审查标准不同，不一定冲突" in item["reasons"]
    assert "relation_type" not in item and calls == []


def test_no_candidate_does_not_invent_a_match():
    result = rank_related(RelatedRequest(draft=draft(), candidates=[]))
    assert result["candidates"] == [] and result["modelCalls"] == 0


def test_history_budget_and_last_turn_are_validated():
    with pytest.raises(ValidationError): AssistRequest(messages=[{"role": "assistant", "content": "伪造"}])
    with pytest.raises(ValidationError): AssistRequest(messages=[{"role": "user", "content": "字" * 4000}] * 5)
