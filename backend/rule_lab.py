"""Local fragment test bench. Deliberately not registered in the production app.

Manual check/source assignment is a test seam, not OCR or semantic IR extraction.
Expected answers never enter the planner or reviewer prompts.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from backend.rule_authoring import AssistRequest, Draft
from backend.rule_lab_trial import TrialRequest

from contract.application.idempotency import canonical_json
from contract.risk.playbooks import build_default_registry
from contract.rule_evidence.binding import RuleEvidenceBinder
from contract.rule_evidence.check_binding import matches_assigned_check
from contract.rule_evidence.java_snapshot import JavaRuleLibrarySnapshot
from contract.rule_evidence.live_snapshot import JavaRuleSnapshotClient
from contract.rule_evidence.models import ReviewRuleSnapshot, RuleEvidencePlanRequest
from contract.rule_evidence.planner import AdaptiveRuleEvidencePlanner
from contract.rule_evidence.reviewer import RuleLibraryReviewer, cached_rule_review, REVIEWER_VERSION
from contract.rule_evidence.shadow import issues_from_plan


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Expected(Strict):
    rule_id: str = Field(min_length=1, max_length=160)
    applicable: bool
    retrieved: bool
    outcome: Literal["RISK", "NO_RISK", "INSUFFICIENT_EVIDENCE", "SKIP"]


class Case(Strict):
    name: str = Field(min_length=1, max_length=160)
    fragment: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(min_length=1, max_length=6000)
    contract_type: str = Field(min_length=1, max_length=160)
    business_role: str = Field(min_length=1, max_length=80)
    perspective: Literal["PARTY_A", "PARTY_B"] = "PARTY_A"
    standard: Literal["neutral", "strong", "weak"] = "neutral"
    include_pending: bool = False
    jurisdiction: str = Field(default="", max_length=128)
    as_of_date: date
    check_codes: list[str] = Field(min_length=1, max_length=8)
    expected: list[Expected] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if not self.fragment.strip():
            raise ValueError("合同片段不能为空白")
        if len({e.rule_id for e in self.expected}) != len(self.expected):
            raise ValueError("预期规则不能重复")
        if len(set(self.check_codes)) != len(self.check_codes):
            raise ValueError("检查项不能重复")
        return self


class Run(Strict):
    run_id: str = Field(pattern=r"^[a-zA-Z0-9-]{8,80}$")
    snapshot_id: str
    mode: Literal["RETRIEVAL", "DEMO", "LIVE"] = "RETRIEVAL"
    case: Case


class RelatedInput(Strict):
    draft: Draft
    searchTerms: list[str] = Field(default_factory=list, max_length=12)


class SaveInput(Strict):
    request_key: str = Field(pattern=r"^[a-zA-Z0-9-]{8,80}$")
    draft: Draft


DEMO_RULES = [ReviewRuleSnapshot(
    rule_id=f"demo-{n}", code=f"DEMO-{n}", version=1, tenant_id="42",
    review_direction="预付款与担保", name=name, contract_type_path=["设备采购合同"],
    party_stance="买受方", review_standard="neutral", rule_type="dedicated",
    source="user", status="active", content=content,
    review_method="核对预付款比例及相应保护条件，缺少比例时明确证据不足。",
) for n, name, content in [
    (1, "预付款比例与保函", "我方为买受方，设备采购预付款超过30%时，必须取得银行保函。"),
    (2, "预付款比例与审批", "我方为买受方，设备采购预付款超过50%时，必须取得负责人批准。"),
]]

# Explicit, finite offline fixtures; never an alternative general-purpose reviewer.
DEMO_FRAGMENTS = [
    ("40%未提供保函", "预付款为合同总价的40%，无需提供银行保函，未经负责人批准。", "RISK", "NO_RISK"),
    ("40%已有保函", "预付款为合同总价的40%，卖方已提供银行保函，未经负责人批准。", "NO_RISK", "NO_RISK"),
    ("30%边界", "预付款为合同总价的30%，无需提供银行保函，未经负责人批准。", "NO_RISK", "NO_RISK"),
    ("20%低于阈值", "预付款为合同总价的20%，无需提供银行保函，未经负责人批准。", "NO_RISK", "NO_RISK"),
    ("比例缺失", "预付款比例另行协商，无需提供银行保函，未经负责人批准。", "INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"),
    ("60%两条规则同时使用", "预付款为合同总价的60%，无需提供银行保函，未经负责人批准。", "RISK", "RISK"),
]


def demo_cases():
    result = []
    for name, fragment, a, b in DEMO_FRAGMENTS:
        result.append(Case(name=name, fragment=fragment, contract_type="设备采购合同",
            business_role="买受方", as_of_date=date.today(), check_codes=["CF-005"],
            expected=[Expected(rule_id=f"demo-{n}", applicable=True, retrieved=True, outcome=outcome)
                      for n, outcome in [(1, a), (2, b)]]))
    for field, value, name in [("business_role", "出卖方", "卖方不适用"),
                               ("contract_type", "劳动合同", "合同类型不适用")]:
        data = result[0].model_dump()
        data.update({field: value, "name": name,
                     "expected": [Expected(rule_id=r.rule_id, applicable=False, retrieved=False, outcome="SKIP")
                                  for r in DEMO_RULES]})
        result.append(Case.model_validate(data))
    return result


class DemoRuntime:
    async def complete_with_usage(self, **kwargs):
        payload = json.loads(kwargs["messages"][0]["content"])
        decisions = []
        for task in payload["rules"]:
            index = next((i for i, rule in enumerate(DEMO_RULES)
                          if rule.content == task["rule_content"]), None)
            source = next(iter(payload["contract_sources"]), None)
            row = next((row for row in DEMO_FRAGMENTS if source and row[1] == source["text"]), None)
            if index is None or row is None:
                raise ValueError("离线替身只支持内置样例原文")
            decisions.append(dict(evidence_id=task["evidence_id"], outcome=row[2 + index],
                title=task["rule_name"], reason="内置固定响应；仅验证链路，不代表 AI 判断质量。",
                suggestion="补充本条规则要求的保护措施。",
                primary_evidence_source_ids=[source["source_id"]]))
        return SimpleNamespace(content=json.dumps({"decisions": decisions}, ensure_ascii=False),
                               prompt_tokens=0, completion_tokens=0)


def fragment_plan(case, run_id):
    checks_by_code = {c.check_code: c for c in build_default_registry().checks}
    if any(code not in checks_by_code for code in case.check_codes):
        raise ValueError("未知检查项")
    # A deliberately explicit fragment adapter. No fabricated IR or extraction result.
    checks = [checks_by_code[code] for code in case.check_codes]
    source = SimpleNamespace(source_id="fragment-1", block_id="fragment-1", char_start=0,
        char_end=len(case.fragment), quoted_text=case.fragment,
        quoted_text_hash="sha256:" + hashlib.sha256(case.fragment.encode()).hexdigest(),
        allowed_check_codes=case.check_codes)
    return SimpleNamespace(review_id="rule-lab-" + run_id, generation_id="fragment-v1",
        perspective=case.perspective, plan_hash=digest(case.model_dump(mode="json", exclude={"expected", "name"})),
        contexts=[SimpleNamespace(check_specs=checks, evidence_sources=[source])])


async def execute(run, snapshot, *, runtime=None, cache_directory=None, model_id="offline-fixture"):
    case = run.case
    rule_ids = {r.rule_id for r in snapshot["rules"]}
    if any(e.rule_id not in rule_ids for e in case.expected):
        raise ValueError("预期规则不存在于当前快照，请重新选择")
    plan = fragment_plan(case, run.run_id)
    request = RuleEvidencePlanRequest(review_id=plan.review_id, generation_id=plan.generation_id,
        tenant_id=snapshot["tenant_id"], contract_type=case.contract_type, perspective=case.perspective,
        business_role=case.business_role, review_standard=case.standard, preview_pending=case.include_pending,
        jurisdiction=case.jurisdiction or None, review_as_of_date=case.as_of_date,
        source_version=snapshot["source_version"], frozen_snapshot_hash=snapshot["snapshot_hash"],
        issues=issues_from_plan(plan))
    eligible = [r for r in snapshot["rules"] if AdaptiveRuleEvidencePlanner._applicable(r, request)]
    request = RuleEvidencePlanRequest.model_validate({**request.model_dump(), "rules": eligible})
    bundle = RuleEvidenceBinder().bind(AdaptiveRuleEvidencePlanner(check_policy=matches_assigned_check).plan(request))
    review = None
    if run.mode != "RETRIEVAL":
        if run.mode == "DEMO":
            if snapshot["kind"] != "DEMO":
                raise ValueError("离线替身只支持内置样例库，请使用仅检索模式或已启用的真实模型")
            runtime = DemoRuntime() if runtime is None else runtime
        if runtime is None:
            raise ValueError("真实模型未启用")
        async def evaluate():
            return await RuleLibraryReviewer(runtime, max_calls=2).review(
                {"bundle": bundle.model_dump(mode="json"), "business_role": case.business_role},
                plan, tenant_id=snapshot["tenant_id"], model_id=model_id, mode="PREVIEW" if case.include_pending else "ACTIVE")
        review = await cached_rule_review(cache_directory, {
            "run_id": run.run_id, "bundle": bundle.bundle_hash, "plan": plan.plan_hash,
            "model": model_id, "mode": run.mode, "reviewer_version": REVIEWER_VERSION},
            evaluate) if cache_directory else await evaluate()
    evidence = {item.rule.rule_id: item for item in bundle.evidence}
    decisions = {item.evidence_id: item for item in review.decisions} if review else {}
    eligible_ids = {r.rule_id for r in eligible}
    assertions = []
    for expected in case.expected:
        item = evidence.get(expected.rule_id)
        decision = decisions.get(item.evidence_id) if item else None
        actual_outcome = decision.outcome if decision else "PENDING" if item and item.check_codes else "SKIP"
        values = {"applicable": expected.rule_id in eligible_ids, "retrieved": item is not None, "outcome": actual_outcome}
        for stage in values:
            wanted, actual = getattr(expected, stage), values[stage]
            status = "BLOCKED" if actual == "PENDING" else "PASS" if wanted == actual else "FAIL"
            assertions.append(dict(rule_id=expected.rule_id, stage=stage, expected=wanted, actual=actual, status=status))
    unexpected = [item.rule.rule_id for item in bundle.evidence if item.rule.rule_id not in {e.rule_id for e in case.expected}]
    statuses = {a["status"] for a in assertions}
    overall = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses or (review and review.pending_evidence_ids) else "PASS"
    if not case.expected:
        overall = "OBSERVED" if review and review.status in {"COMPLETED", "NO_APPLICABLE_RULES"} else "BLOCKED"
    return dict(run_id=run.run_id, case=case.model_dump(mode="json"), mode=run.mode,
        verdict=overall, real_model_requested=run.mode == "LIVE", assertions=assertions,
        validation_scope="TEST_CASE_EXPECTATIONS_ONLY",
        snapshot={k: v for k, v in snapshot.items() if k != "rules"},
        counts={"total": len(snapshot["rules"]), "applicable": len(eligible), "retrieved": len(evidence),
                "bound": sum(bool(e.check_codes) for e in bundle.evidence)},
        unexpected_rule_ids=unexpected, relation_status="UNVERIFIED_NO_PERSISTED_RELATIONS",
        bundle=bundle.model_dump(mode="json"), review=review.model_dump(mode="json") if review else None,
        limitations=["人工指定检查项与合同片段；未覆盖 OCR、语义抽取、完整合同审查。",
                     "关联未验证；多条规则同时命中不代表存在规则关系。",
                     "PASS 仅针对已填写的预期断言，额外命中的规则须另行检查。"])


def create_app(*, cache_directory=None):
    app = FastAPI(title="本地规则测试台", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    snapshots = {}
    cache_directory = cache_directory or Path(".local/rule-lab-cache")
    library_id = None
    library_store = None

    def add_snapshot(rules, tenant_id, source_version, kind, snapshot_hash=None):
        if any(r.tenant_id not in {"0", tenant_id} for r in rules) or len({r.rule_id for r in rules}) != len(rules):
            raise ValueError("规则存在重复 ID 或跨租户数据")
        key = uuid4().hex
        snapshots[key] = dict(rules=rules, tenant_id=tenant_id, source_version=source_version, kind=kind,
            snapshot_id=key, snapshot_hash=snapshot_hash or digest([r.model_dump(mode="json") for r in rules]))
        if len(snapshots) > 8:
            victim = next(k for k in snapshots if k not in {demo_id, library_id})
            del snapshots[victim]
        return key

    demo_id = add_snapshot(DEMO_RULES, "42", "rule-lab-demo-v1", "DEMO")

    def reload_library():
        nonlocal library_id
        if library_store is None:
            raise HTTPException(400, "现有规则来源未配置")
        rules, manifest = library_store.load()
        library_id = add_snapshot(rules, library_store.tenant_id, manifest["source_version"] + "+local-test",
                                  "LOCAL_TEST_LIBRARY")
        snapshots[library_id]["baseline_count"] = manifest["record_count"]
        snapshots[library_id]["baseline_hash"] = manifest["snapshot_hash"]
        snapshots[library_id]["baseline_source"] = manifest.get("source", "existing-rule-export")
        return library_id

    if os.getenv("RULE_LAB_LIBRARY_DIR"):
        from backend.rule_lab_library import TestRuleStore
        library_store = TestRuleStore(os.environ["RULE_LAB_LIBRARY_DIR"],
            os.getenv("RULE_LAB_DATABASE", ".local/rule-lab/rules.sqlite3"), os.getenv("RULE_LAB_TENANT_ID", "42"))
        reload_library()

    def get_snapshot(key):
        if key not in snapshots:
            raise HTTPException(410, "快照已过期或服务已重启，请重新加载规则")
        return snapshots[key]

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        from fastapi.responses import JSONResponse
        if request.headers.get("X-Rule-Lab-Request") != "1":
            return JSONResponse({"detail": "仅允许测试台请求"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:19155", "http://localhost:19155"}:
            return JSONResponse({"detail": "来源不允许"}, status_code=403)
        # Bounded streaming read, including chunked requests.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32 * 1024 * 1024:
                return JSONResponse({"detail": "请求超过 32 MB"}, status_code=413)
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/lab/config")
    def config():
        from backend.rule_lab_runtime import live_status
        enabled, model_status = live_status()
        initial_cases = []
        default_rule_query = ""
        if library_id:
            matches = [r for r in snapshots[library_id]["rules"] if r.name == "预付款比例与担保机制"
                       and r.party_stance == "买受方" and r.review_standard == "neutral" and "采购合同" in r.contract_type_path]
            if len(matches) == 1:
                default_rule_query = matches[0].code
                initial_cases = [Case(name="现有规则 · 40%预付款且无担保", fragment="预付款为合同总价的40%，无需提供银行保函或履约保证金。",
                    contract_type="采购合同", business_role="买受方", as_of_date=date.today(), check_codes=["CF-005"], include_pending=True,
                    expected=[Expected(rule_id=matches[0].rule_id, applicable=True, retrieved=True, outcome="RISK")]).model_dump(mode="json")]
        return dict(demo_snapshot_id=demo_id, cases=[c.model_dump(mode="json") for c in demo_cases()],
            default_snapshot_id=library_id or demo_id, library_snapshot_id=library_id,
            library_cases=initial_cases, default_rule_query=default_rule_query,
            model_status=model_status, authoring_enabled=enabled and library_store is not None,
            checks=[{"code": c.check_code, "title": c.title} for c in build_default_registry().checks],
            live_enabled=enabled,
            java_enabled=all(os.getenv(k) for k in ("RULE_LAB_JAVA_URL", "RULE_LAB_JAVA_TOKEN", "RULE_LAB_TENANT_ID")))

    @app.get("/lab/snapshots/{key}")
    def catalog(key: str, q: str = "", offset: int = 0):
        source = get_snapshot(key)
        filtered = [r for r in source["rules"] if q.casefold() in (r.name + r.code + r.content).casefold()]
        start = max(0, offset)
        from collections import Counter
        return {**{k: v for k, v in source.items() if k != "rules"}, "total": len(source["rules"]),
                "statuses": dict(Counter(r.status for r in source["rules"])),
                "matched": len(filtered), "rules": [r.model_dump(mode="json") for r in filtered[start:start + 200]]}

    @app.post("/lab/library/reload")
    def reload_rules():
        return {"snapshot_id": reload_library()}

    @app.post("/lab/authoring/assist")
    async def author_rule(payload: AssistRequest):
        from backend.rule_authoring import assist, validate_answer
        from backend.rule_authoring_form import explicit_change
        from backend.rule_lab_runtime import build_runtime
        if library_store is None:
            raise HTTPException(400, "请配置测试规则库")
        change = explicit_change(payload.draft, payload.messages[-1].content)
        try:
            if change:
                draft, label = change
                answer = validate_answer(json.dumps(dict(reply=f"已修改{label}，请确认卡片。", draft=draft,
                    questions=[], searchTerms=[]), ensure_ascii=False), payload)
                return {**answer.model_dump(), "usage": {"promptTokens": 0, "completionTokens": 0}}
            return await assist(payload, build_runtime(library_store.tenant_id))
        except Exception as exc:
            raise HTTPException(502, "AI 规则编写失败，输入已保留；未自动重试或保存。") from exc

    @app.post("/lab/authoring/related")
    def related_rules(payload: RelatedInput):
        from backend.rule_authoring import Candidate, RelatedRequest, rank_related
        if library_id is None:
            raise HTTPException(400, "请先加载现有规则库")
        terms = [s.strip().casefold() for s in payload.searchTerms if s.strip()]
        if not terms:
            terms = [payload.draft.name.strip().casefold(), payload.draft.reviewDirection.strip().casefold()]
        terms = [s for s in terms if s]
        rules = [r for r in snapshots[library_id]["rules"] if any(t in (r.name + r.review_direction + r.content).casefold() for t in terms)]
        # Prioritize the confirmed variant before the bounded text-similarity pass.
        # Otherwise the first 300 rows of the full library can exclude the buyer's
        # procurement rules in favor of unrelated contract families and roles.
        def priority(rule):
            return (int(bool(payload.draft.reviewStandard) and rule.review_standard == payload.draft.reviewStandard),
                    int(bool(payload.draft.partyStance) and AdaptiveRuleEvidencePlanner._stance_matches(rule.party_stance, payload.draft.partyStance)),
                    int(bool(set(payload.draft.contractTypePath) & set(rule.contract_type_path))))
        rules.sort(key=priority, reverse=True)
        candidates = [Candidate(id=r.rule_id, code=r.code, name=r.name, content=r.content,
            reviewDirection=r.review_direction, partyStance=r.party_stance or "", reviewStandard=r.review_standard,
            status=r.status, version=r.version, contractTypePath=r.contract_type_path) for r in rules[:300]]
        result = rank_related(RelatedRequest(draft=payload.draft, searchTerms=payload.searchTerms, candidates=candidates))
        result["truncated"] = len(rules) > 300
        return result

    @app.post("/lab/authoring/save")
    def save_rule(payload: SaveInput):
        if library_store is None:
            raise HTTPException(400, "请配置测试规则库")
        try:
            saved = library_store.save(payload.draft, payload.request_key)
            reload_library()
            return saved
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/lab/authoring/rules/{rule_id}")
    def read_saved_rule(rule_id: str):
        if library_store is None:
            raise HTTPException(400, "请配置测试规则库")
        try:
            return library_store.get(rule_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/lab/trial/rules")
    def trial_rules():
        if library_store is None:
            raise HTTPException(400, "测试规则库未配置")
        with library_store.connect() as db:
            rows = db.execute("SELECT rule_json,created_at FROM lab_rules ORDER BY created_at DESC LIMIT 100").fetchall()
        return {"rules": [library_store.view(ReviewRuleSnapshot.model_validate_json(row[0]), row[1]) for row in rows],
                "library_count": len(snapshots[library_id]["rules"]) if library_id else 0}

    @app.post("/lab/trial")
    async def try_rule(payload: TrialRequest):
        from backend.rule_lab_trial import trial
        from backend.rule_lab_runtime import build_runtime
        if library_store is None or not config()["live_enabled"]:
            raise HTTPException(400, "测试规则库或真实 AI 尚未启用")
        # Read the latest saved data; the trial then retains this exact snapshot.
        key = reload_library()
        rule = next((r for r in snapshots[key]["rules"] if r.rule_id == payload.rule_id), None)
        if rule is None:
            raise HTTPException(404, "规则不存在，请重新选择已保存的规则")
        try:
            return await trial(payload, snapshots[key], rule, build_runtime(library_store.tenant_id),
                               cache_directory, os.environ["RULE_LAB_MODEL_ID"])
        except FileExistsError as exc:
            raise HTTPException(409, "同一次试用仍在运行或状态未知，没有自动重复调用 AI") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/lab/import")
    async def import_rules(request: Request):
        try:
            data = await request.json()
            if data.get("schema_version") == "1.0":
                wire = JavaRuleLibrarySnapshot.model_validate(data)
                source = [data["schema_version"], data["source_version"], data["tenant_id"], data["as_of_date"], data["rules"]]
                expected = "sha256:" + hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                if expected != wire.snapshot_hash:
                    raise ValueError("Java 快照哈希不匹配")
                key = add_snapshot(wire.rules, wire.tenant_id, wire.source_version, "IMPORTED_JAVA", wire.snapshot_hash)
            else:
                class Import(Strict):
                    schema_version: Literal["rule-lab-1"]
                    tenant_id: str = Field(min_length=1, max_length=160)
                    source_version: str = Field(min_length=1, max_length=160)
                    rules: list[ReviewRuleSnapshot] = Field(min_length=1, max_length=50000)
                wire = Import.model_validate(data)
                key = add_snapshot(wire.rules, wire.tenant_id, wire.source_version, "MANUAL_IMPORT")
            return {"snapshot_id": key}
        except (ValueError, AttributeError) as exc:
            raise HTTPException(422, "导入无效：请检查格式、租户、重复 ID 和快照哈希") from exc

    @app.post("/lab/java/{task_id}")
    async def load_java(task_id: str):
        if not config()["java_enabled"] or not task_id.isdigit():
            raise HTTPException(400, "Java 快照连接未配置或任务 ID 无效")
        try:
            frozen = await JavaRuleSnapshotClient(os.environ["RULE_LAB_JAVA_URL"], os.environ["RULE_LAB_JAVA_TOKEN"]).load(
                task_id, os.environ["RULE_LAB_TENANT_ID"])
            key = add_snapshot(list(frozen.rules), os.environ["RULE_LAB_TENANT_ID"],
                frozen.manifest["source_version"], "JAVA_FROZEN_TASK", frozen.manifest["snapshot_hash"])
            return {"snapshot_id": key}
        except Exception as exc:
            raise HTTPException(502, "无法读取任务的冻结快照；请检查服务配置、任务和租户。未回退到样例库。") from exc

    @app.post("/lab/run")
    async def run_case(value: Run):
        runtime = None
        model_id = "offline-fixture"
        if value.mode == "LIVE":
            if not config()["live_enabled"]:
                raise HTTPException(403, "真实模型未启用")
            from backend.rule_lab_runtime import build_runtime
            model_id = os.environ["RULE_LAB_MODEL_ID"]
            runtime = build_runtime(get_snapshot(value.snapshot_id)["tenant_id"])
        try:
            return await execute(value, get_snapshot(value.snapshot_id), runtime=runtime,
                                 cache_directory=cache_directory, model_id=model_id)
        except FileExistsError as exc:
            raise HTTPException(409, "该次请求仍在执行或上次执行状态未知；没有自动重复调用模型") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


app = create_app()
