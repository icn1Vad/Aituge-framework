from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from proof.domain.numbering import (
    ARTICLE_START_RE,
    MarkerKind,
    chinese_number_to_int,
    detect_numbering_marker,
)


QUALITY_REPORT_VERSION = "policy-quality-v4"
CONFLICT_TYPES = (
    "numeric_conflict",
    "authority_conflict",
    "process_conflict",
    "rule_reversal",
)
_ARTICLE_LABEL_RE = ARTICLE_START_RE


def build_policy_quality_report(
    policy: dict[str, Any],
    clauses: list[dict[str, Any]],
    *,
    policy_summary: dict[str, Any] | None = None,
    semantic_audit: dict[str, Any] | None = None,
    semantic_findings: list[dict[str, Any]] | None = None,
    conflict_audit: dict[str, Any] | None = None,
    conflict_findings: list[dict[str, Any]] | None = None,
    intra_conflict_audit: dict[str, Any] | None = None,
    intra_conflict_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    occurrences = _collect_number_occurrences(clauses)
    duplicate_findings = _duplicate_number_findings(occurrences)
    missing_findings = _missing_number_findings(occurrences)
    mixed_findings = _mixed_structure_findings(policy)
    semantic_items = [
        {
            "type": item.get("category") or "semantic_ambiguity",
            "id": item["id"],
            "message": item["problem"],
            "suggestion": item["suggestion"],
        }
        for item in (semantic_findings or [])
    ]
    findings = [*duplicate_findings, *missing_findings, *mixed_findings, *semantic_items]
    counts = {
        code: sum(item["type"] == code for item in findings)
        for code in (
            "duplicate_number",
            "missing_number",
            "mixed_structure",
            "semantic_ambiguity",
            "executability_gap",
        )
    }
    counts = {"total": len(findings), **counts}
    audit_state = semantic_audit or {"status": "disabled", "error_message": None}
    conflict_state = conflict_audit or {
        "status": "disabled" if audit_state.get("status") == "disabled" else "pending",
        "error_message": None,
    }
    conflict_items = list(conflict_findings or [])
    conflict_counts = {
        code: sum(item.get("conflict_type") == code for item in conflict_items)
        for code in CONFLICT_TYPES
    }
    conflict_counts = {"total": len(conflict_items), **conflict_counts}
    intra_conflict_state = intra_conflict_audit or {
        "status": conflict_state.get("status"),
        "error_message": conflict_state.get("error_message"),
    }
    intra_conflict_items = list(intra_conflict_findings or [])
    intra_conflict_counts = {
        code: sum(item.get("conflict_type") == code for item in intra_conflict_items)
        for code in CONFLICT_TYPES
    }
    intra_conflict_counts = {"total": len(intra_conflict_items), **intra_conflict_counts}
    summary_state = policy_summary or {
        "status": "disabled" if audit_state.get("status") == "disabled" else "pending",
        "error_message": None,
        "content": None,
    }
    semantic_status = audit_state.get("status")
    conflict_status = conflict_state.get("status")
    intra_conflict_status = intra_conflict_state.get("status")
    summary_status = summary_state.get("status")
    if "failed" in {semantic_status, conflict_status, intra_conflict_status}:
        report_status = "failed"
    elif (
        semantic_status in {"completed", "disabled", "not_requested"}
        and conflict_status in {"completed", "disabled", "not_requested"}
        and intra_conflict_status in {"completed", "disabled", "not_requested"}
        and summary_status in {"completed", "failed", "disabled", "not_requested"}
    ):
        report_status = "completed"
    else:
        report_status = "running"
    return {
        "report_version": QUALITY_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "id": policy["id"],
            "title": policy["title"],
            "version": policy.get("version"),
            "status": policy.get("status"),
            "document_id": policy.get("document_id"),
            "structure_profile": policy.get("structure_profile"),
        },
        "clause_count": len(clauses),
        "report_status": report_status,
        "can_confirm": (
            semantic_status in {"completed", "disabled", "not_requested"}
            and conflict_status in {"completed", "disabled", "not_requested"}
            and intra_conflict_status in {"completed", "disabled", "not_requested"}
        ),
        "has_findings": bool(findings or conflict_items or intra_conflict_items),
        "finding_counts": counts,
        "findings": findings,
        "policy_summary": summary_state,
        "semantic_audit": audit_state,
        "conflict_audit": conflict_state,
        "conflict_counts": conflict_counts,
        "conflict_findings": conflict_items,
        "intra_conflict_audit": intra_conflict_state,
        "intra_conflict_counts": intra_conflict_counts,
        "intra_conflict_findings": intra_conflict_items,
    }


def _collect_number_occurrences(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for clause in clauses:
        unit_type = clause.get("unit_type") or "article"
        if unit_type == "article":
            match = _ARTICLE_LABEL_RE.match(clause.get("clause_no_raw") or "")
            if match:
                number = chinese_number_to_int(match.group(1))
                occurrences.append(
                    _occurrence("article", (number,), clause, clause["clause_no_raw"], ("article",))
                )
            continue
        if unit_type == "decimal_outline":
            seen_in_unit: set[tuple[tuple[int, ...], int]] = set()
            for line_no, line in enumerate((clause.get("text") or "").splitlines(), start=1):
                marker = detect_numbering_marker(line)
                if marker is None or marker.kind not in {MarkerKind.DECIMAL, MarkerKind.ARABIC_HEADING}:
                    continue
                key = (marker.path, line_no)
                if key in seen_in_unit:
                    continue
                seen_in_unit.add(key)
                occurrences.append(
                    _occurrence(
                        "decimal_outline",
                        marker.path,
                        clause,
                        marker.normalized,
                        ("decimal_outline",),
                        line_no=line_no,
                    )
                )
            continue
        if unit_type == "chinese_outline":
            marker = detect_numbering_marker(clause.get("clause_no_raw") or "")
            if marker and marker.path:
                parent = tuple(clause.get("heading_path") or [])
                occurrences.append(
                    _occurrence(
                        "chinese_outline",
                        marker.path,
                        clause,
                        clause["clause_no_raw"],
                        ("chinese_outline", *parent),
                    )
                )
    return occurrences


def _duplicate_number_findings(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in occurrences:
        grouped[(*item["scope"], *item["path"])].append(item)
    findings = []
    for items in grouped.values():
        if len(items) < 2:
            continue
        label = items[0]["label"]
        findings.append(
            {
                "type": "duplicate_number",
                "message": f"编号 {label} 出现 {len(items)} 次。",
                "number": label,
                "occurrences": [
                    {
                        "clause_ordinal": item["clause_ordinal"],
                        "clause_no_raw": item["clause_no_raw"],
                        "line_no": item.get("line_no"),
                    }
                    for item in items
                ],
            }
        )
    return findings


def _missing_number_findings(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = _missing_article_numbers(occurrences)
    decimal_paths = {
        item["path"]
        for item in occurrences
        if item["unit_type"] == "decimal_outline" and item["path"]
    }
    paths_with_ancestors = set(decimal_paths)
    for path in decimal_paths:
        paths_with_ancestors.update(path[:depth] for depth in range(1, len(path)))
    siblings: dict[tuple[int, ...], set[int]] = defaultdict(set)
    for path in paths_with_ancestors:
        siblings[path[:-1]].add(path[-1])
    for parent, values in sorted(siblings.items()):
        ordered = sorted(values)
        for previous, current in zip(ordered, ordered[1:]):
            if current <= previous + 1:
                continue
            missing = [(*parent, number) for number in range(previous + 1, current)]
            findings.append(
                {
                    "type": "missing_number",
                    "message": (
                        f"编号 {_format_decimal((*parent, previous))} 之后为 "
                        f"{_format_decimal((*parent, current))}，可能缺少 "
                        f"{'、'.join(_format_decimal(path) for path in missing)}。"
                    ),
                    "previous_number": _format_decimal((*parent, previous)),
                    "next_number": _format_decimal((*parent, current)),
                    "missing_numbers": [_format_decimal(path) for path in missing],
                }
            )
    return findings


def _missing_article_numbers(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    articles = [item for item in occurrences if item["unit_type"] == "article"]
    findings: list[dict[str, Any]] = []
    if not articles:
        return findings
    previous = articles[0]
    for current in articles[1:]:
        previous_number = previous["path"][0]
        current_number = current["path"][0]
        if current_number == previous_number:
            previous = current
            continue
        if current_number < previous_number:
            previous = current
            continue
        if current_number > previous_number + 1:
            missing = list(range(previous_number + 1, current_number))
            findings.append(
                {
                    "type": "missing_number",
                    "message": (
                        f"{previous['label']} 之后为 {current['label']}，可能缺少 "
                        f"{'、'.join(_format_article(number) for number in missing)}。"
                    ),
                    "previous_number": previous["label"],
                    "next_number": current["label"],
                    "missing_numbers": [_format_article(number) for number in missing],
                    "previous_clause_ordinal": previous["clause_ordinal"],
                    "next_clause_ordinal": current["clause_ordinal"],
                }
            )
        previous = current
    return findings


def _mixed_structure_findings(policy: dict[str, Any]) -> list[dict[str, Any]]:
    if policy.get("structure_profile") != "mixed":
        return []
    diagnostics = policy.get("structure_diagnostics") or {}
    return [
        {
            "type": "mixed_structure",
            "message": "文档包含多个独立编号结构区域。",
            "profiles": diagnostics.get("profiles_detected") or [],
            "regions": diagnostics.get("regions") or [],
        }
    ]


def _occurrence(
    unit_type: str,
    path: tuple[int, ...],
    clause: dict[str, Any],
    label: str,
    scope: tuple[Any, ...],
    *,
    line_no: int | None = None,
) -> dict[str, Any]:
    return {
        "unit_type": unit_type,
        "path": path,
        "label": label,
        "scope": scope,
        "clause_ordinal": clause["clause_ordinal"],
        "clause_no_raw": clause["clause_no_raw"],
        "line_no": line_no,
    }


def _format_decimal(path: tuple[int, ...]) -> str:
    return ".".join(str(value) for value in path)


def _format_article(number: int) -> str:
    return f"第{number}条"
