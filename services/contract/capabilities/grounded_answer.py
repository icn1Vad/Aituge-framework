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


class GroundedChatMessage(StrictModel):
    role: Literal["USER", "ASSISTANT"]
    content: str = Field(min_length=1, max_length=8000)


class GroundedAnswerTaskInput(StrictModel):
    schema_version: Literal["1.0"]
    mode: Literal["REPORT", "CHAT"]
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    instruction: str | None = Field(default=None, max_length=4000)
    question: str | None = Field(default=None, max_length=8000)
    conversation_history: list[GroundedChatMessage] = Field(
        default_factory=list,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "GroundedAnswerTaskInput":
        if self.mode == "REPORT":
            if self.question is not None or self.conversation_history:
                raise ValueError("REPORT mode does not accept question or conversation_history")
            return self
        if self.question is None or not self.question.strip():
            raise ValueError("CHAT mode requires a non-empty question")
        return self


class GroundedAnswerPipelineContext(StrictModel):
    task_input: GroundedAnswerTaskInput
    artifacts: dict[str, Any] = Field(default_factory=dict)


class GroundedCitationDraft(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=200)


class GroundedAnswerDraft(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["REPORT", "CHAT"]
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
    mode: Literal["REPORT", "CHAT"]
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_markdown: str = Field(min_length=1, max_length=100_000)
    references: list[GroundedReference] = Field(default_factory=list, max_length=500)


class GroundedAnswerMaterializationError(ValueError):
    """Raised when a model-authored citation cannot be grounded deterministically."""


def _is_absence_evidence(evidence: dict[str, Any] | None) -> bool:
    """Return whether evidence records a verified absence without a source anchor."""

    return isinstance(evidence, dict) and evidence.get("evidence_type") == "ABSENCE"


def _strip_declared_absence_docrefs(
    *,
    content_markdown: str,
    absence_citation_ids: set[str],
) -> str:
    """Keep absence findings as prose instead of emitting non-locatable docrefs."""

    def replace(match: re.Match[str]) -> str:
        evidence_id = match.group("evidence_id")
        if evidence_id in absence_citation_ids:
            return match.group("label")
        return match.group(0)

    return DOCREF_PATTERN.sub(replace, content_markdown)


def _can_backfill_docref(
    *,
    evidence_id: str,
    label: str,
    evidence: dict[str, Any] | None,
    finding_ids: set[str],
) -> bool:
    """Return whether a missing Markdown link can be restored without guessing a location."""

    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", evidence_id)
        or not label.strip()
        or any(character in label for character in ("[", "]", chr(13), chr(10)))
        or not isinstance(evidence, dict)
        or evidence.get("evidence_type") not in {"TEXT_QUOTE", "CONTEXT"}
    ):
        return False
    block_id = evidence.get("block_id")
    finding_id = evidence.get("finding_id")
    page_number = evidence.get("page_number")
    char_start = evidence.get("char_start")
    char_end = evidence.get("char_end")
    quoted_text = evidence.get("quoted_text")
    quoted_text_hash = evidence.get("quoted_text_hash")
    return (
        isinstance(block_id, str)
        and bool(block_id.strip())
        and isinstance(finding_id, str)
        and finding_id in finding_ids
        and (page_number is None or isinstance(page_number, int) and page_number >= 1)
        and isinstance(char_start, int)
        and isinstance(char_end, int)
        and char_start >= 0
        and char_end > char_start
        and isinstance(quoted_text, str)
        and len(quoted_text) == char_end - char_start
        and isinstance(quoted_text_hash, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", quoted_text_hash) is not None
    )


def _backfill_missing_docrefs(
    *,
    content_markdown: str,
    citations: list[GroundedCitationDraft],
    evidence_by_id: dict[str, dict[str, Any]],
    finding_ids: set[str],
) -> str:
    """Append deterministic docrefs only when the authoritative evidence is locatable."""

    marker_ids = {
        match.group("evidence_id") for match in DOCREF_PATTERN.finditer(content_markdown)
    }
    citation_labels = {citation.evidence_id: citation.label for citation in citations}
    missing_ids = set(citation_labels) - marker_ids
    if not missing_ids:
        return content_markdown

    missing_markers: list[tuple[str, str]] = []
    for evidence_id in sorted(missing_ids):
        label = citation_labels[evidence_id]
        if not _can_backfill_docref(
            evidence_id=evidence_id,
            label=label,
            evidence=evidence_by_id.get(evidence_id),
            finding_ids=finding_ids,
        ):
            raise GroundedAnswerMaterializationError(
                f"missing Markdown marker for non-locatable evidence '{evidence_id}'"
            )
        missing_markers.append((evidence_id, label))

    return "{}{}参考依据：{}{}".format(
        content_markdown.rstrip(),
        chr(10) * 2,
        chr(10),
        chr(10).join(
            f"- [{label}](#docref-{evidence_id})"
            for evidence_id, label in missing_markers
        ),
    )


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

    absence_citation_ids = {
        citation.evidence_id
        for citation in draft.citations
        if _is_absence_evidence(evidence_by_id.get(citation.evidence_id))
    }
    locatable_citations = [
        citation
        for citation in draft.citations
        if citation.evidence_id not in absence_citation_ids
    ]
    content_markdown = _strip_declared_absence_docrefs(
        content_markdown=draft.content_markdown,
        absence_citation_ids=absence_citation_ids,
    )
    content_markdown = _backfill_missing_docrefs(
        content_markdown=content_markdown,
        citations=locatable_citations,
        evidence_by_id=evidence_by_id,
        finding_ids=finding_ids,
    )
    marker_pairs = [
        (match.group("evidence_id"), match.group("label"))
        for match in DOCREF_PATTERN.finditer(content_markdown)
    ]
    marker_ids = {item[0] for item in marker_pairs}
    citation_ids = {item.evidence_id for item in locatable_citations}
    if marker_ids != citation_ids:
        missing = sorted(citation_ids - marker_ids)
        dangling = sorted(marker_ids - citation_ids)
        raise GroundedAnswerMaterializationError(
            f"Markdown markers and citations differ; missing={missing}, dangling={dangling}"
        )
    contract_version_id = review_result.get("contract_version_id")
    result_hash = review_result.get("result_hash")
    if not isinstance(contract_version_id, str) or not isinstance(result_hash, str):
        raise GroundedAnswerMaterializationError(
            "review result is missing contract_version_id or result_hash"
        )

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
