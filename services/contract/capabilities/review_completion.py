"""Describe unfinished review work without discarding validated siblings.

Execution completion is not a statement that the contract is legally compliant.
The ledger remains authoritative; this projection never creates a Finding or
changes FAILED/INSUFFICIENT_EVIDENCE into a no-risk decision.
"""
from __future__ import annotations

from typing import Any


def review_completion(extended: Any) -> dict[str, Any]:
    records = {r.check_code: r for r in extended.review_records}
    units = [*extended.base_bundle.units, *extended.horizontal_units]
    checks = {c.check_code: (unit, c) for unit in units for c in unit.check_results}
    expected = sorted(set(extended.check_codes) | set(records) | set(checks))
    pending = []
    for code in expected:
        record = records.get(code)
        reasons = list(record.unresolved_reasons) if record is not None else ["REVIEW_RECORD_MISSING"]
        if record is not None:
            if record.execution_status != "COMPLETED":
                reasons.append("CHECK_EXECUTION_INCOMPLETE")
            if record.judgement in {"UNRESOLVED", "INSUFFICIENT_EVIDENCE"}:
                reasons.append("JUDGEMENT_UNRESOLVED")
        unit, check = checks.get(code, (None, None))
        if check is None or check.status == "FAILED":
            reasons.append("CHECK_EXECUTION_INCOMPLETE")
        if check is not None and check.reason_code == "INSUFFICIENT_EVIDENCE":
            reasons.append("INSUFFICIENT_EVIDENCE")
        if reasons:
            pending.append({
                "check_code": code,
                "unit_id": record.unit_id if record is not None else getattr(unit, "unit_id", "unknown"),
                "review_question": record.review_question if record is not None else "部分检查未返回完整记录",
                "batch_ids": sorted(record.batch_ids) if record is not None else [],
                "reason_codes": sorted(set(reasons)),
            })
    incomplete_units = sorted(u.unit_id for u in units if u.status != "COMPLETED")
    return {
        "version": "1.0",
        "status": "PARTIAL" if pending or incomplete_units or extended.status != "COMPLETED" else "COMPLETED",
        "check_count": len(expected),
        "completed_check_count": len(expected) - len(pending),
        "pending_check_count": len(pending),
        "incomplete_units": incomplete_units,
        "pending_checks": pending,
    }


def result_overview(finding_count: int, completion: dict[str, Any] | None) -> str:
    if completion is not None and completion["status"] == "PARTIAL":
        questions = list(dict.fromkeys(item["review_question"] for item in completion["pending_checks"]))
        detail = "待复核范围：" + "；".join(questions) + "。" if questions else "部分审查域未完成。"
        return (
            f"部分审查待完成：已保留{finding_count}项有依据的风险结果。"
            f"{completion['pending_check_count']}个检查项待复核；未完成部分不代表无风险。"
            + detail
        )
    return (
        f"发现{finding_count}项需要人工复核的合同事项。"
        if finding_count else "未发现需要人工复核的实质合同风险。"
    )


def final_result_overview(result: dict[str, Any], completion: dict[str, Any] | None) -> str:
    """Project only presentation text after rule cards have been merged."""
    overview = result_overview(len(result["findings"]), completion)
    rules = result.get("rule_review") or {}
    pending = set(rules.get("pending_evidence_ids", [])) | {
        item["evidence_id"] for item in rules.get("decisions", [])
        if item["outcome"] == "INSUFFICIENT_EVIDENCE"
    }
    if pending or rules.get("status") in {"PARTIAL", "SELECTION_UNRESOLVED"}:
        # A zero-card result with unfinished supplemental review is not clearance.
        if not result["findings"] and (completion or {}).get("status") != "PARTIAL":
            overview = "本次未返回风险项，仍有审查内容待复核。"
        if pending:
            overview += f"规则库审查另有{len(pending)}项待复核；未完成部分不代表无风险。"
        elif rules.get("status") == "SELECTION_UNRESOLVED":
            overview += "规则适用范围尚未确定，规则库审查待复核。"
        else:
            overview += "规则库审查部分内容待复核，未完成部分不代表无风险。"
    return overview
