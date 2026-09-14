"""Connect the existing review plan to rule-library evidence without changing findings."""
from __future__ import annotations

from datetime import date
import hashlib
import re
import time

from contract.rule_evidence.binding import RuleEvidenceBinder
from contract.rule_evidence.models import RuleEvidenceIssue, RuleEvidencePlanRequest
from contract.rule_evidence.planner import AdaptiveRuleEvidencePlanner
from contract.rule_evidence.snapshot import shared_rule_snapshot
from contract.rule_evidence.check_binding import matches_assigned_check


CONCEPTS = (
    "付款", "支付", "价款", "发票", "税费", "利息", "验收", "质量", "交付", "工期",
    "变更", "范围", "配合", "分包", "转让", "质保", "维修", "主体", "授权", "签署",
    "生效", "期限", "保密", "知识产权", "数据", "侵权", "违约", "赔偿", "解除",
    "终止", "返还", "不可抗力", "争议", "管辖", "仲裁", "定义", "附件", "通知",
)


def issues_from_plan(plan):
    """Use the real check assignments and their scoped contract evidence."""
    collected = {}
    for context in plan.contexts:
        for check in context.check_specs:
            key = (check.domain, check.check_code)
            query = check.title + "：" + check.review_question
            facts = [source.quoted_text for source in context.evidence_sources
                     if check.check_code in source.allowed_check_codes]
            if key not in collected:
                collected[key] = (query, [])
            collected[key][1].extend(facts)
    return [RuleEvidenceIssue(
        issue_id="rule-issue-" + hashlib.sha256(
            (plan.plan_hash + ":" + code).encode()).hexdigest()[:32],
        domain=domain, query=query,
        facts=list(dict.fromkeys(facts))[:50],
        required_concepts=[term for term in CONCEPTS if term in query],
        check_codes=[code],
    ) for (domain, code), (query, facts) in sorted(collected.items())]


class RuleLibraryShadow:
    """A separately enabled observation path; pending rules never become usable."""
    def __init__(self, directory=None, *, snapshot=None):
        self.snapshot = snapshot if snapshot is not None else shared_rule_snapshot(directory)

    def selectors(self, value):
        text = "\n".join(block.text for block in value.source_blocks)
        names = {name for rule in self.snapshot.rules for name in rule.contract_type_path}
        # Only an unambiguous title match is accepted automatically.
        titles = {name for name in names if name and name in text[:500]}
        titles = {name for name in titles if not any(name != other and name in other for other in titles)}
        side = "甲方" if str(getattr(value.perspective, "value", value.perspective)) == "PARTY_A" else "乙方"
        roles = {rule.party_stance for rule in self.snapshot.rules if rule.party_stance}
        matches = set()
        for role in roles:
            for pattern in (rf"{side}\s*[（(]\s*{re.escape(role)}\s*[）)]",
                            rf"{re.escape(role)}\s*[（(]\s*{side}\s*[）)]"):
                if re.search(pattern, text):
                    matches.add(role)
        # An explicit affirmative purchase fact also identifies the two roles;
        # never infer buyer/seller merely from PARTY_A/B or company names.
        if len(titles) == 1 and "采购" in next(iter(titles)):
            for fact in re.finditer(
                r'(?:^|[。；;\n“「])\s*(甲方|乙方)\s*向\s*(甲方|乙方)\s*(?:采购|购买)', text
            ):
                buyer, seller = fact.groups()
                if buyer == seller:
                    continue
                role = "买受方" if side == buyer else "出卖方"
                if role in roles:
                    matches.add(role)
        return (next(iter(titles)) if len(titles) == 1 else None,
                next(iter(matches)) if len(matches) == 1 else None)

    def evaluate(self, plan, *, tenant_id, contract_type_name=None, business_role=None, business_roles=None,
                 review_standard="neutral", review_as_of_date=None, jurisdiction=None, preview_pending=True,
                 contract_type_aliases=()):
        started = time.perf_counter()
        issues = issues_from_plan(plan)
        if not issues:
            return {"mode": "SHADOW", "status": "NO_CHECKS", "model_calls": 0}
        selectors = RuleEvidencePlanRequest(
            review_id=plan.review_id, generation_id=plan.generation_id, tenant_id=tenant_id,
            contract_type=plan.contract_type, contract_type_aliases=sorted(set([*contract_type_aliases,
                *([contract_type_name] if contract_type_name else [])])),
            perspective=str(getattr(plan.perspective, "value", plan.perspective)),
            business_role=business_role, business_roles=business_roles or [],
            review_standard=review_standard, preview_pending=preview_pending,
            review_as_of_date=review_as_of_date or getattr(self.snapshot, "as_of_date", None) or date.today(), jurisdiction=jurisdiction,
            source_version=self.snapshot.manifest["source_version"], issues=issues,
        )
        request = self.snapshot.select(selectors)
        bundle = RuleEvidenceBinder().bind(
            AdaptiveRuleEvidencePlanner(check_policy=matches_assigned_check).plan(request))
        return {
            "mode": "SHADOW", "status": bundle.status, "plan_hash": plan.plan_hash,
            "contract_type_name": contract_type_name, "business_role": business_role,
            "business_roles": request.business_roles,
            "review_standard": review_standard, "review_as_of_date": request.review_as_of_date.isoformat(),
            "source_record_count": len(self.snapshot.rules), "eligible_rule_count": len(request.rules),
            "selection_warnings": [name for name, value in
                                   (("CONTRACT_TYPE_UNRESOLVED", request.contract_type_aliases),
                                    ("BUSINESS_ROLE_UNRESOLVED", request.business_roles)) if not value],
            "bundle": bundle.model_dump(mode="json"),
            "check_evidence": {code: [item.evidence_id for item in bundle.evidence if code in item.check_codes]
                               for code in sorted({code for issue in issues for code in issue.check_codes})},
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "model_calls": 0, "added_model_tokens": 0, "affects_findings": False,
        }


def observe_rule_library(shadow, plan, value, tenant_id):
    """Opt-in stage observation, isolated from the authoritative result path."""
    if shadow is None:
        return None
    try:
        contract_type_name, business_role = shadow.selectors(value)
        return shadow.evaluate(plan, tenant_id=tenant_id,
                               contract_type_name=contract_type_name, business_role=business_role,
                               review_standard=value.review_attitude.lower())
    except Exception as exc:
        # Diagnostics identify the failure class without copying contract text or credentials.
        return {"mode": "SHADOW", "status": "FAILED", "error_type": type(exc).__name__,
                "model_calls": 0, "affects_findings": False}
