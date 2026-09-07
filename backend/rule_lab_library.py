"""Read-only baseline plus durable, explicitly separate local test rules."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.rule_authoring import Draft
from contract.rule_evidence.models import ReviewRuleSnapshot
from contract.rule_evidence.snapshot import LocalRuleSnapshot


class TestRuleStore:
    __test__ = False

    def __init__(self, directory, database, tenant_id):
        self.directory, self.database, self.tenant_id = directory, Path(database), tenant_id
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS lab_rules (request_key TEXT PRIMARY KEY, payload TEXT NOT NULL, rule_id TEXT UNIQUE NOT NULL, rule_json TEXT NOT NULL, created_at TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.database, timeout=10)

    def load(self):
        baseline = LocalRuleSnapshot(self.directory)
        with self.connect() as db:
            rows = db.execute("SELECT rule_json FROM lab_rules ORDER BY created_at, rule_id").fetchall()
        rules = [*baseline.rules, *(ReviewRuleSnapshot.model_validate_json(r[0]) for r in rows)]
        return rules, baseline.manifest

    def save(self, draft: Draft, request_key):
        if any(not getattr(draft, field).strip() for field in
               ["name", "reviewDirection", "partyStance", "reviewStandard", "ruleType", "content", "reviewMethod"]):
            raise ValueError("请补齐规则名称、方向、立场、类型、内容和审查方式")
        if draft.ruleType == "dedicated" and not draft.contractTypePath:
            raise ValueError("专用规则必须选择合同分类")
        if draft.ruleType == "statutory" and not draft.referenceBasis:
            raise ValueError("法定规则必须填写参考依据")
        payload = draft.model_dump_json()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload, rule_json, created_at FROM lab_rules WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError("相同保存请求不能携带不同规则")
                return self.view(ReviewRuleSnapshot.model_validate_json(existing[1]), existing[2])
            rule_id = "lab-" + uuid4().hex
            rule = ReviewRuleSnapshot(rule_id=rule_id, code="LAB-" + rule_id[4:16], version=1,
                tenant_id=self.tenant_id, review_direction=draft.reviewDirection, name=draft.name,
                contract_type_path=draft.contractTypePath, party_stance=draft.partyStance,
                review_standard=draft.reviewStandard, rule_type=draft.ruleType, source="ai_assisted",
                reference_basis=draft.referenceBasis or None, content=draft.content, review_method=draft.reviewMethod,
                status="active", jurisdiction=draft.jurisdiction or None,
                effective_from=draft.effectiveFrom or None, effective_to=draft.effectiveTo or None)
            created = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO lab_rules VALUES (?,?,?,?,?)", (request_key, payload, rule_id, rule.model_dump_json(), created))
        return self.view(rule, created)

    def get(self, rule_id):
        with self.connect() as db:
            row = db.execute("SELECT rule_json,created_at FROM lab_rules WHERE rule_id=?", (rule_id,)).fetchone()
        if row is None:
            raise ValueError("测试库中不存在该规则")
        return self.view(ReviewRuleSnapshot.model_validate_json(row[0]), row[1])

    @staticmethod
    def view(rule, created):
        return dict(id=rule.rule_id, code=rule.code, version=rule.version, name=rule.name,
            reviewDirection=rule.review_direction, contractTypePath=rule.contract_type_path,
            partyStance=rule.party_stance or "", reviewStandard=rule.review_standard, ruleType=rule.rule_type,
            source=rule.source, referenceBasis=rule.reference_basis or "", content=rule.content,
            reviewMethod=rule.review_method, status=rule.status, jurisdiction=rule.jurisdiction or "",
            effectiveFrom=rule.effective_from.isoformat() if rule.effective_from else None,
            effectiveTo=rule.effective_to.isoformat() if rule.effective_to else None,
            createdAt=created, updatedAt=created, createdBy="local-rule-lab")
