"""Grounded contract report/chat schemas and deterministic citation materialization."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DOCREF_PATTERN = re.compile(
    r"\[(?P<label>[^\]\r\n]+)\]\(#docref-(?P<evidence_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,159})\)"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroundedAnswerTaskInput(StrictModel):
    schema_version: Literal["1.0"]
    mode: Literal["REPORT"]
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    instruction: str | None = Field(default=None, max_length=4000)


class GroundedAnswerPipelineContext(StrictModel):
    task_input: GroundedAnswerTaskInput
    artifacts: dict[str, Any] = Field(default_factory=dict)


class GroundedCitationDraft(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=200)


class GroundedAnswerDraft(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["REPORT"]
    content_markdown: str = Field(min_length=1, max_length=100_000)
    citations: list[GroundedCitationDraft] = Field(default_factory=list, max_length=500)


class GroundedReference(StrictModel):
    reference_id: str = Field(pattern=r"^docref-[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    label: str = Field(min_length=1, max_length=200)
    evidence_id: str = Field(min_length=1, max_length=160)
    finding_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    chunk_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_range(self) -> "GroundedReference":
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.char_end - self.char_start != len(self.quoted_text):
            raise ValueError("reference range length must match quoted_text")
        return self


class GroundedAnswerResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["REPORT"]
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_markdown: str = Field(min_length=1, max_length=100_000)
    references: list[GroundedReference] = Field(default_factory=list, max_length=500)


class GroundedAnswerMaterializationError(ValueError):
    """Raised when a model-authored citation cannot be grounded deterministically."""


def materialize_grounded_answer(
    *,
    task_input: GroundedAnswerTaskInput,
    draft: GroundedAnswerDraft,
    review_result: dict[str, Any],
) -> GroundedAnswerResult:
    """Validate Markdown markers and replace citation drafts with authoritative anchors."""

    if draft.mode != task_input.mode:
        raise GroundedAnswerMaterializationError("draft mode does not match task mode")
    if review_result.get("review_id") != task_input.review_id:
        raise GroundedAnswerMaterializationError("review result does not match review_id")

    findings = review_result.get("findings")
    evidences = review_result.get("evidences")
    if not isinstance(findings, list) or not isinstance(evidences, list):
        raise GroundedAnswerMaterializationError("review result is missing findings or evidences")
    finding_ids = {
        item.get("finding_id")
        for item in findings
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    evidence_by_id = {
        item["evidence_id"]: item
        for item in evidences
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }

    marker_pairs = [
        (match.group("evidence_id"), match.group("label"))
        for match in DOCREF_PATTERN.finditer(draft.content_markdown)
    ]
    marker_ids = {item[0] for item in marker_pairs}
    citation_ids = {item.evidence_id for item in draft.citations}
    if marker_ids != citation_ids:
        missing = sorted(citation_ids - marker_ids)
        dangling = sorted(marker_ids - citation_ids)
        raise GroundedAnswerMaterializationError(
            f"Markdown markers and citations differ; missing={missing}, dangling={dangling}"
        )
    citation_labels_by_id: dict[str, set[str]] = {}
    for citation in draft.citations:
        citation_labels_by_id.setdefault(citation.evidence_id, set()).add(citation.label)
    for evidence_id, label in marker_pairs:
        if label not in citation_labels_by_id[evidence_id]:
            raise GroundedAnswerMaterializationError(
                f"citation label does not match Markdown marker for '{evidence_id}'"
            )

    contract_version_id = review_result.get("contract_version_id")
    result_hash = review_result.get("result_hash")
    if not isinstance(contract_version_id, str) or not isinstance(result_hash, str):
        raise GroundedAnswerMaterializationError(
            "review result is missing contract_version_id or result_hash"
        )

    content_markdown = draft.content_markdown
    references: list[GroundedReference] = []
    referenced_ids: set[str] = set()
    for evidence_id, label in marker_pairs:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            content_markdown = content_markdown.replace(
                f"[{label}](#docref-{evidence_id})",
                label,
            )
            continue
        if evidence.get("evidence_type") == "ABSENCE":
            content_markdown = content_markdown.replace(
                f"[{label}](#docref-{evidence_id})",
                label,
            )
            continue
        if evidence_id in referenced_ids:
            continue
        if evidence.get("evidence_type") not in {"TEXT_QUOTE", "CONTEXT"}:
            raise GroundedAnswerMaterializationError(
                f"evidence '{evidence_id}' is not locatable text evidence"
            )
        finding_id = evidence.get("finding_id")
        if finding_id not in finding_ids:
            raise GroundedAnswerMaterializationError(
                f"evidence '{evidence_id}' has no matching finding"
            )
        try:
            references.append(
                GroundedReference(
                    reference_id=f"docref-{evidence_id}",
                    label=label,
                    evidence_id=evidence_id,
                    finding_id=finding_id,
                    document_id=task_input.document_id,
                    contract_version_id=contract_version_id,
                    chunk_id=evidence["block_id"],
                    block_id=evidence["block_id"],
                    page_number=evidence.get("page_number"),
                    char_start=evidence["char_start"],
                    char_end=evidence["char_end"],
                    quoted_text=evidence["quoted_text"],
                    quoted_text_hash=evidence["quoted_text_hash"],
                )
            )
            referenced_ids.add(evidence_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise GroundedAnswerMaterializationError(
                f"evidence '{evidence_id}' has an invalid source anchor"
            ) from exc

    return GroundedAnswerResult(
        mode=task_input.mode,
        review_id=task_input.review_id,
        document_id=task_input.document_id,
        contract_version_id=contract_version_id,
        result_hash=result_hash,
        content_markdown=content_markdown,
        references=references,
    )
