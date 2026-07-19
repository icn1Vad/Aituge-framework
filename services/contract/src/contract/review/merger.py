from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence
from typing import Any

from contract.api.models import Evidence, Finding
from contract.callback.models import ReviewStageResult
from contract.errors import ContractError


_RISK_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def merge_review_stage_results(
    stages: Sequence[ReviewStageResult],
) -> tuple[list[Finding], list[Evidence]]:
    finding_payloads: list[dict[str, Any]] = []
    evidence_payloads: list[dict[str, Any]] = []
    finding_id_payload: dict[str, dict[str, Any]] = {}

    for stage in stages:
        for finding in stage.findings:
            payload = finding.model_dump(mode="json")
            identity_payload = {key: value for key, value in payload.items() if key != "evidence_ids"}
            existing = finding_id_payload.get(finding.finding_id)
            if existing is not None and existing != identity_payload:
                raise _invalid("Finding ID is duplicated with different content")
            finding_id_payload[finding.finding_id] = identity_payload
            finding_payloads.append(payload)
        evidence_payloads.extend(evidence.model_dump(mode="json") for evidence in stage.evidences)

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for finding in finding_payloads:
        groups.setdefault(_finding_key(finding), []).append(finding)

    selected_findings: dict[str, dict[str, Any]] = {}
    finding_id_mapping: dict[str, str] = {}
    for candidates in groups.values():
        winner = min(candidates, key=_finding_preference)
        selected = dict(winner)
        selected["evidence_ids"] = []
        selected_findings[selected["finding_id"]] = selected
        for candidate in candidates:
            finding_id_mapping[candidate["finding_id"]] = selected["finding_id"]

    evidence_id_payload: dict[str, dict[str, Any]] = {}
    evidence_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for evidence in evidence_payloads:
        mapped_finding_id = finding_id_mapping.get(evidence["finding_id"])
        if mapped_finding_id is None:
            raise _invalid("Evidence references an unknown finding")
        mapped = dict(evidence)
        mapped["finding_id"] = mapped_finding_id
        existing = evidence_id_payload.get(mapped["evidence_id"])
        if existing is not None and existing != mapped:
            raise _invalid("Evidence ID is duplicated with different content")
        evidence_id_payload[mapped["evidence_id"]] = mapped
        evidence_groups.setdefault(_evidence_key(mapped), []).append(mapped)

    selected_evidences: dict[str, dict[str, Any]] = {}
    for candidates in evidence_groups.values():
        selected = min(candidates, key=lambda item: item["evidence_id"])
        selected_evidences[selected["evidence_id"]] = selected
        selected_findings[selected["finding_id"]]["evidence_ids"].append(selected["evidence_id"])

    findings = [
        Finding.model_validate(value)
        for _, value in sorted(selected_findings.items(), key=lambda item: item[0])
    ]
    evidences = [
        Evidence.model_validate(value)
        for _, value in sorted(selected_evidences.items(), key=lambda item: item[0])
    ]
    return findings, evidences


def _finding_key(value: dict[str, Any]) -> tuple[str, ...]:
    return (
        value["category"],
        value["perspective"],
        _normalized_text(value["our_party"]),
        _normalized_text(value["counterparty"]),
        _normalized_text(value["title"]),
        _normalized_text(value["issue"]),
    )


def _finding_preference(value: dict[str, Any]) -> tuple[int, int, str, str]:
    descriptive_length = sum(
        len(value[field]) for field in ("title", "issue", "impact_to_our_party", "suggestion")
    )
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (-_RISK_RANK[value["risk_level"]], -descriptive_length, canonical, value["finding_id"])


def _evidence_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value["finding_id"],
        value["evidence_type"],
        value["block_id"],
        value["page_number"],
        value["char_start"],
        value["char_end"],
        value["quoted_text"],
        value["quoted_text_hash"],
        _normalized_text(value["checked_scope"] or ""),
        _normalized_text(value["verification_note"] or ""),
    )


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _invalid(message: str) -> ContractError:
    return ContractError("RESULT_INVALID", message, status_code=422)
