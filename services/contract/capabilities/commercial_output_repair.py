"""Bounded check-local format repair; already accepted checks are never regenerated."""
from __future__ import annotations

import copy
import json


def build_check_repair(*, baseline, error, request_context, check_schema):
    checks = baseline.get("check_results") if isinstance(baseline, dict) else None
    if not isinstance(checks, list):
        checks = None
    valid_ids = [item.get("check_code") for item in checks or [] if isinstance(item, dict)]
    unique = bool(checks) and all(isinstance(code, str) for code in valid_ids) and len(valid_ids) == len(checks) == len(set(valid_ids))
    targets = list(error.check_codes) if unique and error.check_codes else []
    selected = [item for item in checks or [] if item.get("check_code") in targets] if targets else checks
    context = copy.deepcopy(request_context)
    if targets:
        context["assigned_check_specs"] = [item for item in context["assigned_check_specs"] if item["check_code"] in targets]
        context["decision_policies"] = {key: value for key, value in context["decision_policies"].items() if key in targets}
        if "CF-005" not in targets:
            context.pop("cf005_candidate", None)
        refs = set()
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"ir_ref", "evidence_ref"} and isinstance(item, str):
                        refs.add(item)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(selected)
        candidate = context.get("cf005_candidate") or {}
        refs.update(candidate.get("candidate_ir_refs") or [])
        refs.update(candidate.get("candidate_evidence_refs") or [])
        # If no source has been selected (e.g. a no-risk note), retain the already
        # bounded financial context. Never fetch the entire contract for a repair.
        if refs:
            projected = context.get("projected_ir", {})
            context["projected_ir"] = {group: [item for item in entries if item.get("ir_ref") in refs]
                                       for group, entries in projected.items()}
            context["source_excerpts"] = [item for item in context.get("source_excerpts", []) if item.get("evidence_ref") in refs]
    context.pop("output_contract", None)
    context.pop("legal_evidence_catalog", None)  # this is repair, not a second legal review
    schema = copy.deepcopy(check_schema)
    definitions = schema.pop("$defs", {})
    output_schema = {"type": "object", "additionalProperties": False,
                     "required": ["check_results"], "properties": {"check_results": {
                         "type": "array", "items": schema}}, "$defs": definitions}
    return {"task": "REPAIR_FAILED_CHECKS_ONLY", "target_check_codes": targets,
            "original_checks": selected, "errors": error.validation_issues,
            "context": context, "output_schema": output_schema,
            "constraints": [
                "只返回target_check_codes指定的检查项，其他已通过检查项由程序原样保留",
                "允许补充或更正目标检查项的decision_note，包括已有非空说明；说明需对应提供的事实，禁止套用已审查无风险模板",
                "允许修复明确的格式问题；status、Finding及其原文Evidence仍须保留，不能新增或删除风险",
                "不得把非空findings改为空数组",
                "不得伪造证据、保障措施、原文或业务判断；无法修复则保留失败，不得清空结果规避错误",
            ]}, targets


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
    if not set(by_code).issubset(original):
        raise ValueError("Repair introduced an unknown check")
    for code, check in by_code.items():
        if code not in targets and check != original[code]:
            raise ValueError("Repair changed an untargeted check")
    merged = copy.deepcopy(baseline)
    merged["check_results"] = [copy.deepcopy(by_code[check["check_code"]])
                               if check["check_code"] in targets else check
                               for check in merged["check_results"]]
    return merged
