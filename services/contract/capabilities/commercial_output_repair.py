"""Bounded check-local format repair; already accepted checks are never regenerated."""
from __future__ import annotations

import copy
import json
import re
from services.contract.capabilities.review_evidence_protocol import scope_direct_repair_context


def infer_repair_targets(error, baseline):
    checks = baseline.get("check_results", []) if isinstance(baseline, dict) else []
    if not isinstance(checks, list):
        return []
    codes = [c.get("check_code") for c in checks if isinstance(c, dict)]
    if len(codes) != len(checks) or any(not isinstance(c, str) for c in codes) or len(set(codes)) != len(codes):
        return []
    targets = set(getattr(error, "check_codes", []))
    cause = error.__cause__
    issues = cause.errors() if hasattr(cause, "errors") else []
    if not hasattr(cause, "errors"):
        targets.update(re.findall(r"\b(?:CF|FVA|PO|ICD|LRE|CCC|MAC)-[0-9]{3}\b", str(error)))
    for issue in issues:
        loc = issue.get("loc", ())
        if len(loc) > 1 and loc[0] == "check_results" and isinstance(loc[1], int) and loc[1] < len(codes):
            targets.add(codes[loc[1]])
    return sorted(targets & set(codes))


def build_check_repair(*, baseline, error, request_context, check_schema):
    checks = baseline.get("check_results") if isinstance(baseline, dict) else None
    if not isinstance(checks, list):
        checks = None
    valid_ids = [item.get("check_code") for item in checks or [] if isinstance(item, dict)]
    unique = bool(checks) and all(isinstance(code, str) for code in valid_ids) and len(valid_ids) == len(checks) == len(set(valid_ids))
    # The list sent to the model MUST be the same one used by merge/preservation.
    # With a valid envelope but an unlocated error, no sibling is certified yet.
    targets = (list(error.check_codes) or infer_repair_targets(error, baseline) or list(valid_ids)) if unique else []
    selected = [item for item in checks or [] if item.get("check_code") in targets] if targets else checks
    context = copy.deepcopy(request_context)
    if targets:
        context = scope_direct_repair_context(context, targets)
        # A rejected reference is NOT a retrieval query. Filtering by the wrong
        # I/A pair removes precisely the alternatives needed to correct it.
        # Retain the original bounded batch catalogue, including both ends of
        # every binding. No new contract retrieval or model call is performed.
    # Reassessment must see the same contract AND legal text. A failed type/ID
    # can coexist with a wrong conclusion; do not freeze that conclusion.
    schema = copy.deepcopy(check_schema)
    definitions = schema.pop("$defs", {})
    output_schema = {"type": "object", "additionalProperties": False,
                     "required": ["check_results"], "properties": {"check_results": {
                         "type": "array", "items": schema}}, "$defs": definitions}
    mode = "REASSESS_TARGET" if "source_selection_contract" in context else repair_mode(error)
    prompt_targets = targets or [s["check_code"] for s in context["assigned_check_specs"]]
    return {"task": "REPAIR_FAILED_CHECKS_ONLY", "repair_mode": mode, "target_check_codes": prompt_targets,
            "original_checks": selected, "errors": error.validation_issues,
            "context": context, "output_schema": output_schema,
            "constraints": [
                "只返回target_check_codes指定的检查项，其他已通过检查项由程序原样保留",
                "允许补充或更正目标检查项的decision_note，包括已有非空说明；说明需对应提供的事实，禁止套用已审查无风险模板",
                ("业务复核模式：允许纠正目标项的判断、风险标题、说明和结论；必须一并纠正矛盾字段，并重新通过原文绑定、立场和范围校验"
                 if mode == "REASSESS_TARGET" else
                 "格式修复模式：保留风险判断和文字；证据绑定错误时可从原始目录重新选择正确配对，不得按相近编号猜测"),
                ("删除被证伪的目标风险时必须说明对应证据与复核原因" if mode == "REASSESS_TARGET" else "不得把非空findings改为空数组"),
                "不得伪造证据、保障措施、原文或业务判断；无法修复则保留失败，不得清空结果规避错误",
            ]}, targets


def repair_mode(error):
    if getattr(error, "validation_stage", None) == "BUSINESS":
        return "REASSESS_TARGET"
    if any(issue.get("stage") == "EVIDENCE" for issue in getattr(error, "validation_issues", [])):
        return "REBIND_EVIDENCE"
    return "FORMAT_ONLY"


def merge_check_repair(baseline, repaired, targets):
    if not targets:
        return repaired
    if not isinstance(repaired, dict) or set(repaired) != {"check_results"}:
        raise ValueError("Repair must return only check_results")
    entries = repaired.get("check_results")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("Repair check_results must be a list of check objects")
    if any(not isinstance(item.get("check_code"), str) for item in entries):
        raise ValueError("Repair requires a string check_code")
    by_code = {item.get("check_code"): item for item in entries}
    original = {item["check_code"]: item for item in baseline["check_results"]}
    if len(by_code) != len(entries) or not set(targets).issubset(by_code):
        raise ValueError("Repair omitted or duplicated a target check")
    if not set(by_code).issubset(set(original) | set(targets)):
        raise ValueError("Repair introduced an unknown check")
    for code, check in by_code.items():
        if code not in targets and check != original[code]:
            raise ValueError("Repair changed an untargeted check")
    # Only the declared envelope survives. Invalid top-level extras are format
    # errors, not accepted sibling judgments that need preservation.
    merged = {"check_results": copy.deepcopy(baseline["check_results"])}
    merged["check_results"] = [copy.deepcopy(by_code[check["check_code"]])
                               if check["check_code"] in targets else check
                               for check in merged["check_results"]]
    merged["check_results"].extend(copy.deepcopy(by_code[code]) for code in targets if code not in original)
    return merged
