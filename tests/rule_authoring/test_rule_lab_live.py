import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from backend.rule_lab import DEMO_RULES, create_app
from backend.rule_lab_library import TestRuleStore
from backend.rule_lab_runtime import provider, live_status
from backend.rule_authoring import Draft


def baseline(tmp_path):
    root = tmp_path / "baseline"
    root.mkdir()
    raw = b"".join((r.model_copy(update={"status": "pending"}).model_dump_json() + "\n").encode() for r in DEMO_RULES)
    (root / "rules.ndjson").write_bytes(raw)
    (root / "manifest.json").write_text(json.dumps(dict(source_version="test-baseline", record_count=2,
        snapshot_hash="sha256:" + hashlib.sha256(raw).hexdigest())))
    return root


def draft():
    return Draft(name="预付款保函", reviewDirection="预付款", contractTypePath=["采购合同"],
        partyStance="买受方", reviewStandard="neutral", ruleType="dedicated", content="预付款超过30%须提供银行保函。",
        reviewMethod="核对预付款比例和保函约定。")


def test_new_key_only_no_gateway_or_legacy_fallback(tmp_path, monkeypatch):
    key = "sk-" + "new-test-value-" * 3
    path = tmp_path / "selected-key.txt"
    path.write_text(key)
    monkeypatch.setenv("RULE_LAB_KEY_FILE", str(path))
    monkeypatch.setenv("RULE_LAB_ENABLE_LIVE", "1")
    monkeypatch.setenv("RULE_LAB_MODEL_ID", "deepseek-v4-flash")
    monkeypatch.setenv("MODEL_SECRET_DEEPSEEK_API_KEY", "sk-old-must-never-be-used")
    monkeypatch.delenv("MODEL_GATEWAY_URL", raising=False)
    resolved = provider().resolve_llm()
    assert resolved.api_key == key and not resolved.via_gateway
    assert resolved.base_url == "https://api.deepseek.com"
    path.unlink()
    assert live_status()[0] is False
    with pytest.raises(ValueError, match="无法读取"):
        provider()
    path.write_text(key)
    monkeypatch.setenv("MODEL_GATEWAY_URL", "http://old-gateway")
    assert live_status()[0] is False


def test_test_library_preserves_baseline_and_survives_restart(tmp_path):
    source = baseline(tmp_path)
    before = (source / "rules.ndjson").read_bytes()
    db = tmp_path / "test.sqlite3"
    store = TestRuleStore(source, db, "42")
    saved = store.save(draft(), "save-request-001")
    assert store.save(draft(), "save-request-001")["id"] == saved["id"]
    changed = draft().model_copy(update={"content": "different"})
    with pytest.raises(ValueError, match="相同保存请求"):
        store.save(changed, "save-request-001")
    restarted = TestRuleStore(source, db, "42")
    rules, manifest = restarted.load()
    assert len(rules) == 3 and manifest["record_count"] == 2
    assert rules[0].status == "pending" and rules[-1].status == "active"
    assert restarted.get(saved["id"]) == saved
    assert (source / "rules.ndjson").read_bytes() == before


def test_authoring_http_reuses_ai_validation_and_persists_separately(tmp_path, monkeypatch):
    source = baseline(tmp_path)
    monkeypatch.setenv("RULE_LAB_LIBRARY_DIR", str(source))
    monkeypatch.setenv("RULE_LAB_DATABASE", str(tmp_path / "rules.sqlite3"))
    calls = []
    class FakeRuntime:
        model_runtime_provider = SimpleNamespace(active_pack=SimpleNamespace(llm=SimpleNamespace(id="test")))
        async def complete_with_usage(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps(dict(reply="请确认", draft=draft().model_dump(),
                questions=[], searchTerms=["预付款"]), ensure_ascii=False), prompt_tokens=10, completion_tokens=20)
    monkeypatch.setattr("backend.rule_lab_runtime.build_runtime", lambda tenant: FakeRuntime())
    c = TestClient(create_app(cache_directory=tmp_path / "cache"), headers={"X-Rule-Lab-Request": "1"})
    conf = c.get("/lab/config").json()
    assert conf["default_snapshot_id"] != conf["demo_snapshot_id"]
    a = c.post("/lab/authoring/assist", json=dict(messages=[{"role": "user", "content": "采购合同预付款超过30%需要银行保函"}],
        draft=Draft().model_dump(), contractTypes=[["采购合同"]]))
    assert a.status_code == 200, a.text
    assert len(calls) == 1 and calls[0]["thinking_override"] is False
    r = c.post("/lab/authoring/related", json=dict(draft=a.json()["draft"], searchTerms=["预付款"]))
    assert r.status_code == 200 and len(r.json()["candidates"]) == 2
    saved = c.post("/lab/authoring/save", json=dict(draft=a.json()["draft"], request_key="api-save-request-1"))
    assert saved.status_code == 200, saved.text
    assert c.get("/lab/authoring/rules/" + saved.json()["id"]).json() == saved.json()
    loaded = c.post("/lab/library/reload", json={}).json()["snapshot_id"]
    catalog = c.get("/lab/snapshots/" + loaded).json()
    assert catalog["total"] == 3 and catalog["statuses"] == {"pending": 2, "active": 1}
