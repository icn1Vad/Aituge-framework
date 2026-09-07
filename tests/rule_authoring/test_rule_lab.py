import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend.rule_lab import (DEMO_RULES, DemoRuntime, Run, create_app, demo_cases, digest, execute)


def snapshot():
    return dict(rules=DEMO_RULES, tenant_id="42", source_version="test-v1", kind="DEMO",
                snapshot_hash=digest([r.model_dump(mode="json") for r in DEMO_RULES]))


def run(case=None, mode="DEMO", runtime=None, **kwargs):
    value = Run(run_id="test-run-0001", snapshot_id="demo", mode=mode, case=case or demo_cases()[0])
    return asyncio.run(execute(value, snapshot(), runtime=runtime, **kwargs))


@pytest.mark.parametrize("case", demo_cases(), ids=lambda c: c.name)
def test_business_fixtures_and_two_rules_survive_same_concept_coverage(case):
    result = run(case)
    assert result["verdict"] == "PASS"
    assert not result["real_model_requested"]
    assert result["relation_status"] == "UNVERIFIED_NO_PERSISTED_RELATIONS"
    if case.expected[0].retrieved:
        assert result["counts"]["retrieved"] == 2
        assert all(0 < e["relevance_score"] < .8 for e in result["bundle"]["evidence"])


def test_expected_answers_never_enter_model_and_wrong_answer_fails():
    class Recording(DemoRuntime):
        async def complete_with_usage(self, **kwargs):
            self.payload = kwargs["messages"][0]["content"]
            return await super().complete_with_usage(**kwargs)
    runtime = Recording()
    case = demo_cases()[0]
    case.expected[0].outcome = "NO_RISK"
    case.name = "secret-expectation-canary"
    result = run(case, runtime=runtime)
    assert result["verdict"] == "FAIL"
    assert "secret-expectation-canary" not in runtime.payload and '"expected"' not in runtime.payload
    assert result["review"]["decisions"][0]["citations"]


def test_retrieval_only_does_not_claim_business_judgment_passed():
    result = run(mode="RETRIEVAL")
    assert result["verdict"] == "BLOCKED"
    assert result["review"] is None
    assert {a["stage"] for a in result["assertions"] if a["status"] == "BLOCKED"} == {"outcome"}


def test_corrupt_quote_blocks_result_and_no_retry():
    class Corrupt(DemoRuntime):
        calls = 0
        async def complete_with_usage(self, **kwargs):
            self.calls += 1
            answer = await super().complete_with_usage(**kwargs)
            payload = json.loads(answer.content)
            payload["decisions"][0]["quotes"][0]["quote"] = "fabricated quotation"
            answer.content = json.dumps(payload)
            return answer
    runtime = Corrupt()
    result = run(runtime=runtime)
    assert result["verdict"] == "BLOCKED" and runtime.calls == 1
    assert result["review"]["diagnostics"] == ["BATCH_FAILED:ValueError"]


def test_offline_fixture_never_guesses_for_custom_fragment():
    case = demo_cases()[0]
    case.fragment = "自定义条款不能用固定样例模型评价"
    assert run(case)["verdict"] == "BLOCKED"


def test_fragment_whitespace_preserved_for_source_offsets():
    from backend.rule_lab import Case
    data = demo_cases()[0].model_dump()
    data["fragment"] = "\n  原文片段  \n"
    assert Case.model_validate(data).fragment == data["fragment"]


def test_java_import_hash_rejects_tampering(tmp_path):
    import hashlib
    client = TestClient(create_app(cache_directory=tmp_path), headers={"X-Rule-Lab-Request": "1"})
    wire = dict(schema_version="1.0", source_version="java-v1", tenant_id="42", as_of_date="2026-09-07",
                rules=[r.model_dump(mode="json") for r in DEMO_RULES])
    source = [wire[k] for k in ["schema_version", "source_version", "tenant_id", "as_of_date", "rules"]]
    wire["snapshot_hash"] = "sha256:" + hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert client.post("/lab/import", json=wire).status_code == 200
    wire["rules"][0]["content"] = "篡改"
    assert client.post("/lab/import", json=wire).status_code == 422


def test_same_request_cache_does_not_repeat_model_but_rechecks_changed_expectations(tmp_path):
    class Count(DemoRuntime):
        calls = 0
        async def complete_with_usage(self, **kwargs):
            self.calls += 1
            return await super().complete_with_usage(**kwargs)
    runtime = Count()
    assert run(runtime=runtime, cache_directory=tmp_path)["verdict"] == "PASS"
    case = demo_cases()[0]
    case.expected[0].outcome = "NO_RISK"
    assert run(case, runtime=runtime, cache_directory=tmp_path)["verdict"] == "FAIL"
    assert runtime.calls == 1


def test_local_http_import_validation_and_disabled_live(tmp_path, monkeypatch):
    monkeypatch.delenv("RULE_LAB_ENABLE_LIVE", raising=False)
    client = TestClient(create_app(cache_directory=tmp_path))
    assert client.get("/lab/config").status_code == 403
    headers = {"X-Rule-Lab-Request": "1"}
    assert client.get("/lab/config", headers={**headers, "Origin": "https://foreign.example"}).status_code == 403
    assert client.get("/lab/config", headers={**headers, "Host": "rebind.example"}).status_code == 400
    client.headers.update(headers)
    config = client.get("/lab/config").json()
    data = dict(run_id="http-run-001", snapshot_id=config["demo_snapshot_id"], mode="LIVE", case=config["cases"][0])
    assert client.post("/lab/run", json=data).status_code == 403
    data["mode"] = "DEMO"
    assert client.post("/lab/run", json=data).json()["verdict"] == "PASS"
    rules = dict(schema_version="rule-lab-1", tenant_id="42", source_version="edited-v2",
                 rules=[r.model_dump(mode="json") for r in DEMO_RULES])
    rules["rules"][0]["content"] = "更新后的规则内容"
    rules["rules"][0]["version"] = 2
    imported = client.post("/lab/import", json=rules).json()["snapshot_id"]
    assert client.get(f"/lab/snapshots/{imported}").json()["rules"][0]["version"] == 2
    data["snapshot_id"] = imported
    assert client.post("/lab/run", json=data).status_code == 422  # no fake model on real imported rules
    data["mode"] = "RETRIEVAL"
    assert client.post("/lab/run", json=data).status_code == 200
    foreign = deepcopy(rules)
    foreign["rules"][0]["tenant_id"] = "99"
    assert client.post("/lab/import", json=foreign).status_code == 422
    duplicate = deepcopy(rules)
    duplicate["rules"].append(duplicate["rules"][0])
    assert client.post("/lab/import", json=duplicate).status_code == 422
    data["case"]["expected"][0]["rule_id"] = "missing"
    assert client.post("/lab/run", json=data).status_code == 422
    assert client.post("/lab/import", json=[1, 2]).status_code == 422
