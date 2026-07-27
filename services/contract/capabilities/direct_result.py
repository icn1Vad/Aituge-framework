"""Materialize the formal result directly from the Extended Risk Bundle."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from contract.api.models import ContractProfile, Finding, ReviewSummary
from contract.callback.models import EvidenceCandidate, FinalizeReviewStageResult
from contract.evidence.validator import materialize_evidence_set


class DirectResultError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_direct_final_stage(
    *,
    bundle: Any,
    contract_profile: ContractProfile,
    business_task_id: str,
    contract_version_id: str,
    blocks: Sequence[Mapping[str, Any]],
) -> FinalizeReviewStageResult:
    if getattr(bundle, "status", None) != "COMPLETED":
        raise DirectResultError(
            "FRAMEWORK_RUN_FAILED",
            "Cannot materialize an incomplete Extended Bundle",
        )

    findings: list[Finding] = []
    candidates: list[EvidenceCandidate] = []
    finding_ids: set[str] = set()
    evidence_ids: set[str] = set()

    for source in sorted(bundle.findings, key=lambda item: item.finding_local_id):
        finding_id = source.finding_local_id
        if finding_id in finding_ids:
            raise DirectResultError(
                "RESULT_INVALID",
                "Direct Bundle contains duplicate Finding IDs",
            )
        finding_ids.add(finding_id)
        source_candidates = list(source.evidence_candidates)
        local_evidence_ids = [item.evidence_local_id for item in source_candidates]
        if len(local_evidence_ids) != len(set(local_evidence_ids)):
            raise DirectResultError(
                "RESULT_INVALID",
                "Direct Finding contains duplicate Evidence IDs",
            )
        for candidate in source_candidates:
            if candidate.evidence_local_id in evidence_ids:
                raise DirectResultError(
                    "RESULT_INVALID",
                    "Direct Bundle contains duplicate Evidence IDs",
                )
            evidence_ids.add(candidate.evidence_local_id)
            candidates.append(
                EvidenceCandidate(
                    evidence_id=candidate.evidence_local_id,
                    finding_id=finding_id,
                    evidence_type=candidate.evidence_type,
                    block_id=candidate.block_id,
                    page_number=candidate.page_number,
                    char_start=candidate.char_start,
                    char_end=candidate.char_end,
                    quoted_text=candidate.quoted_text,
                    quoted_text_hash=candidate.quoted_text_hash,
                    checked_scope=candidate.checked_scope,
                    verification_note=candidate.verification_note,
                )
            )
        findings.append(
            Finding(
                finding_id=finding_id,
                category=source.category,
                risk_level=source.risk_level,
                title=source.title,
                perspective=source.perspective,
                our_party=source.our_party,
                counterparty=source.counterparty,
                issue=source.issue,
                impact_to_our_party=source.impact_to_our_party,
                suggestion=source.suggestion,
                evidence_ids=local_evidence_ids,
            )
        )

    evidences = materialize_evidence_set(
        findings,
        candidates,
        blocks,
        contract_profile,
    )
    counts = Counter(item.risk_level.value for item in findings)
    overview = (
        f"\u53d1\u73b0{len(findings)}\u9879\u9700\u8981\u4eba\u5de5\u590d\u6838\u7684\u5408\u540c\u4e8b\u9879\u3002"
        if findings
        else "\u672a\u53d1\u73b0\u9700\u8981\u4eba\u5de5\u590d\u6838\u7684\u5b9e\u8d28\u5408\u540c\u98ce\u9669\u3002"
    )
    return FinalizeReviewStageResult(
        result_type="FINAL_REVIEW_STAGE_V1",
        schema_version="1.0",
        review_id=bundle.review_id,
        business_task_id=business_task_id,
        contract_version_id=contract_version_id,
        contract_profile=contract_profile,
        summary=ReviewSummary(
            overview=overview,
            high_count=counts["HIGH"],
            medium_count=counts["MEDIUM"],
            low_count=counts["LOW"],
            info_count=counts["INFO"],
        ),
        findings=findings,
        evidences=evidences,
        relationships=[],
    )
