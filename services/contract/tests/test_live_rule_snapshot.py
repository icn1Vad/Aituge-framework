import asyncio
import hashlib
import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest

from contract.rule_evidence.execution import RuleLibraryExecution
from contract.rule_evidence.live_snapshot import JavaRuleSnapshotClient
from risk_test_data import risk_plan_input
from test_rule_evidence_planner import _rule
from test_rule_library_reviewer import FakeModel


def wire(content, tenant="42"):
    rule = _rule("payment", name="付款期限", content=content, status="active", party_stance="买受方").model_copy(update={
        "rule_type": "dedicated", "contract_type_path": ["采购合同"], "jurisdiction": None,
    }).model_dump(mode="json")
    data = {"schema_version": "1.0", "source_version": "java-review-rule-snapshot-v1", "tenant_id": tenant,
            "as_of_date": date.today().isoformat(), "rules": [rule]}
    rehash(data)
    return data


def rehash(data):
    body = [data[k] for k in ("schema_version", "source_version", "tenant_id", "as_of_date", "rules")]
    data["snapshot_hash"] = "sha256:" + hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class RecordingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.prompts = []

    async def complete_with_usage(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return await super().complete_with_usage(**kwargs)


async def run_two_tasks(client, first_id, second_id, tenant, tmp_path):
    model = RecordingModel()
    # One executor stays alive for both requests. No file release or restart.
    executor = RuleLibraryExecution(None, tmp_path / "cache", mode="ACTIVE", snapshot_client=client, runtime_factory=lambda _: model)
    first_shadow = await executor.task_shadow(first_id, tenant)
    first = await executor.run(risk_plan_input(), tenant, "offline-model", task_shadow=first_shadow,
                               contract_type_name="采购合同", business_role="买受方")
    assert first.status == "COMPLETED" and first.decisions
    first_prompts = "\n".join(model.prompts)
    assert "验收后10日" in first_prompts
    value = risk_plan_input().model_copy(update={"review_id": "review-2", "generation_id": "generation-2"})
    second_shadow = await executor.task_shadow(second_id, tenant)
    second = await executor.run(value, tenant, "offline-model", task_shadow=second_shadow,
                                contract_type_name="采购合同", business_role="买受方")
    assert second.status == "COMPLETED" and second.decisions
    assert "验收后20日" in model.prompts[-1] and "验收后10日" not in model.prompts[-1]
    assert second.snapshot_hash != first.snapshot_hash
    calls = model.calls
    replay_shadow = await executor.task_shadow(first_id, tenant)
    replay = await executor.run(risk_plan_input(), tenant, "offline-model", task_shadow=replay_shadow,
                                contract_type_name="采购合同", business_role="买受方")
    assert replay == first and model.calls == calls
    assert first_shadow.snapshot.rules[0].content == replay_shadow.snapshot.rules[0].content
    return {"passed": True, "same_executor": True, "first_snapshot": first.snapshot_hash,
            "second_snapshot": second.snapshot_hash, "model_calls": model.calls,
            "first_rule_content": first_shadow.snapshot.rules[0].content,
            "second_rule_content": second_shadow.snapshot.rules[0].content,
            "replay_preserved_original": True, "model": "offline test double"}


def test_one_executor_reads_each_task_snapshot_and_replays_original(tmp_path):
    versions = {"101": wire("付款应在验收后10日内支付。"), "102": wire("付款应在验收后20日内支付。")}
    def handler(req):
        assert req.headers["X-Internal-Token"] == "test" and req.headers["X-Tenant-Id"] == "42"
        return httpx.Response(200, json=versions[req.url.path.rsplit("/", 1)[-1]])
    client = JavaRuleSnapshotClient("http://java", "test", transport=httpx.MockTransport(handler))
    asyncio.run(run_two_tasks(client, "101", "102", "42", tmp_path))


def test_task_snapshot_and_semantic_multi_role_selection_share_the_same_catalogue(tmp_path, monkeypatch):
    versions = {"101": wire("付款应在验收后10日内支付。"), "102": wire("付款应在验收后20日内支付。")}
    for data in versions.values():
        extra = dict(data["rules"][0], rule_id="entrusting-payment", party_stance="委托方")
        data["rules"].append(extra)
        rehash(data)
    selections = []

    async def select(value, snapshot, **kwargs):
        selections.append((snapshot.manifest["snapshot_hash"], kwargs["declared_roles"]))
        assert {rule.party_stance for rule in snapshot.rules} == {"买受方", "委托方"}
        return {"status": "RESOLVED", "business_roles": ["买受方", "委托方"],
                "contract_types": ["采购合同"], "input_hash": snapshot.manifest["snapshot_hash"]}

    monkeypatch.setattr("contract.rule_evidence.semantic_selection.select_rule_applicability", select)
    client = JavaRuleSnapshotClient("http://java", "test", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=versions[req.url.path.rsplit("/", 1)[-1]])))
    model = RecordingModel()
    executor = RuleLibraryExecution(None, tmp_path / "cache", mode="ACTIVE", snapshot_client=client,
        runtime_factory=lambda _: model, semantic_selection=True)
    assert executor.shadow is None

    async def run():
        results = []
        for task_id in ("101", "102"):
            shadow = await executor.task_shadow(task_id, "42")
            value = risk_plan_input().model_copy(update={"review_id": "review-" + task_id})
            result = await executor.run(value, "42", "offline-model", task_shadow=shadow,
                business_roles=["采购人"], infer_business_role=False)
            assert result.status == "COMPLETED" and result.decisions
            assert set(result.business_roles) == {"买受方", "委托方"}
            assert result.snapshot_hash == shadow.snapshot.manifest["snapshot_hash"]
            results.append(result)
        return results

    results = asyncio.run(run())
    assert [item[0] for item in selections] == [versions[key]["snapshot_hash"] for key in ("101", "102")]
    assert all(item[1] == ["采购人"] for item in selections)
    assert results[0].snapshot_hash != results[1].snapshot_hash
    assert "验收后20日" in model.prompts[-1] and "验收后10日" not in model.prompts[-1]


@pytest.mark.parametrize("failure", ["missing", "unavailable", "foreign", "hash", "duplicate", "oversize", "malformed"])
def test_snapshot_failure_never_falls_back_to_old_rules(failure):
    calls = []
    def handler(req):
        calls.append(req)
        if failure in {"missing", "unavailable"}:
            return httpx.Response(404 if failure == "missing" else 503)
        data = wire("付款期限")
        if failure == "foreign": data["tenant_id"] = "9"
        if failure == "hash": data["rules"][0]["content"] = "被修改"
        if failure == "duplicate": data["rules"].append(data["rules"][0]); rehash(data)
        if failure == "malformed": return httpx.Response(200, text="invalid-json")
        return httpx.Response(200, json=data)
    client = JavaRuleSnapshotClient("http://java", "test", transport=httpx.MockTransport(handler), max_bytes=1 if failure == "oversize" else 100000)
    with pytest.raises((ValueError, httpx.HTTPError)):
        asyncio.run(client.load("101", "42"))
    assert len(calls) == 1


def test_snapshot_uses_creation_date_when_replayed_after_date_changes(tmp_path):
    data = wire("付款应在验收后10日内支付。")
    data["as_of_date"] = "2025-01-01"
    data["rules"][0]["effective_to"] = "2025-12-31"
    rehash(data)
    client = JavaRuleSnapshotClient("http://java", "test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    async def run():
        executor = RuleLibraryExecution(None, tmp_path, mode="ACTIVE", snapshot_client=client, runtime_factory=lambda _: FakeModel())
        shadow = await executor.task_shadow("101", "42")
        return await executor.run(risk_plan_input(), "42", "test", task_shadow=shadow,
                                   contract_type_name="采购合同", business_role="买受方")
    assert asyncio.run(run()).status == "COMPLETED"


@pytest.mark.skipif(not os.getenv("RULE_SNAPSHOT_TEST_CONTROL_DIR"), reason="Opt-in real Java/H2 integration server")
def test_real_java_task_creation_sql_and_http_feed_the_review_model(tmp_path):
    control = Path(os.environ["RULE_SNAPSHOT_TEST_CONTROL_DIR"])
    tasks = json.loads((control / "tasks.json").read_text())
    client = JavaRuleSnapshotClient(f"http://127.0.0.1:{tasks['port']}", "offline-rule-snapshot-token")
    result = asyncio.run(run_two_tasks(client, tasks["first"], tasks["second"], "7", tmp_path))
    (control / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
