from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from contract.api.models import ContractProfile, Evidence, Finding
from contract.callback.models import EvidenceCandidate
from contract.errors import ContractError


def materialize_evidence_set(
    findings: Sequence[Finding],
    candidates: Sequence[EvidenceCandidate],
    blocks: Sequence[Mapping[str, Any]],
    profile: ContractProfile,
) -> list[Evidence]:
    block_by_id = {str(block["block_id"]): block for block in blocks}
    evidence_values: list[Evidence] = []
    for candidate in candidates:
        payload = candidate.model_dump(mode="json")
        if candidate.evidence_type.value != "ABSENCE":
            block = block_by_id.get(candidate.block_id or "")
            if block is None:
                raise _invalid("Evidence block is outside this contract generation")
            assert candidate.char_start is not None and candidate.char_end is not None
            if candidate.char_end > len(block["text"]) or candidate.char_start >= candidate.char_end:
                raise _invalid("Evidence character range is outside its block")
            quoted_text = block["text"][candidate.char_start : candidate.char_end]
            quoted_text_hash = "sha256:" + hashlib.sha256(quoted_text.encode("utf-8")).hexdigest()
            if candidate.quoted_text is not None and candidate.quoted_text != quoted_text:
                raise _invalid("Evidence candidate text does not match its block")
            if candidate.quoted_text_hash is not None and candidate.quoted_text_hash != quoted_text_hash:
                raise _invalid("Evidence candidate hash does not match its block")
            if candidate.page_number is not None and candidate.page_number != block["page_number"]:
                raise _invalid("Evidence candidate page does not match its block")
            payload.update(
                page_number=block["page_number"],
                quoted_text=quoted_text,
                quoted_text_hash=quoted_text_hash,
            )
        evidence_values.append(Evidence.model_validate(payload))
    validate_evidence_set(findings, evidence_values, blocks, profile)
    return evidence_values


def validate_evidence_set(
    findings: Sequence[Finding],
    evidences: Sequence[Evidence],
    blocks: Sequence[Mapping[str, Any]],
    profile: ContractProfile,
) -> None:
    finding_by_id = {finding.finding_id: finding for finding in findings}
    evidence_by_id = {evidence.evidence_id: evidence for evidence in evidences}
    if len(finding_by_id) != len(findings):
        raise _invalid("Finding IDs must be unique")
    if len(evidence_by_id) != len(evidences):
        raise _invalid("Evidence IDs must be unique")

    for finding in findings:
        if (
            finding.perspective != profile.perspective
            or finding.our_party != profile.our_party
            or finding.counterparty != profile.counterparty
            or not finding.evidence_ids
        ):
            raise _invalid("Finding perspective, parties, or evidence is invalid")
        for evidence_id in finding.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None or evidence.finding_id != finding.finding_id:
                raise _invalid("Finding evidence link is invalid")

    block_by_id = {str(block["block_id"]): block for block in blocks}
    for evidence in evidences:
        finding = finding_by_id.get(evidence.finding_id)
        if finding is None or evidence.evidence_id not in finding.evidence_ids:
            raise _invalid("Evidence finding link is invalid")
        if evidence.evidence_type.value == "ABSENCE":
            if not evidence.checked_scope or not evidence.checked_scope.strip():
                raise _invalid("ABSENCE evidence requires a checked scope")
            if not evidence.verification_note or not evidence.verification_note.strip():
                raise _invalid("ABSENCE evidence requires a verification note")
            continue

        block = block_by_id.get(evidence.block_id or "")
        if block is None:
            raise _invalid("Evidence block is outside this contract generation")
        assert evidence.char_start is not None and evidence.char_end is not None
        assert evidence.quoted_text is not None and evidence.quoted_text_hash is not None
        if evidence.char_end > len(block["text"]) or evidence.char_start >= evidence.char_end:
            raise _invalid("Evidence character range is outside its block")
        quoted_text = block["text"][evidence.char_start : evidence.char_end]
        if quoted_text != evidence.quoted_text:
            raise _invalid("Evidence text does not match its block")
        expected_hash = "sha256:" + hashlib.sha256(quoted_text.encode("utf-8")).hexdigest()
        if evidence.quoted_text_hash != expected_hash:
            raise _invalid("Evidence text hash does not match its block")
        if evidence.page_number is not None and evidence.page_number != block["page_number"]:
            raise _invalid("Evidence page does not match its block")


def _invalid(message: str) -> ContractError:
    return ContractError("EVIDENCE_INVALID", message, status_code=422)
