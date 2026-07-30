from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

from rapidfuzz.distance import Levenshtein

from proof.versioning import format_policy_version

DEFAULT_VERSION = "v1.0.0"
SIMILARITY_DECISION_REQUIRED = "decision_required"
SIMILARITY_CLEAR = "clear"


@dataclass(frozen=True, slots=True)
class SimilarityThresholds:
    title: float = 0.75
    edit: float = 0.80
    jaccard: float = 0.79
    containment: float = 0.98
    length_ratio: float = 0.80
    clause_coverage: float = 0.80


def normalize_similarity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(
        char for char in normalized if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def normalized_text_hash(value: str) -> str:
    return hashlib.sha256(normalize_similarity_text(value).encode("utf-8")).hexdigest()


def similarity_metrics(
    *,
    title: str,
    normalized_title: str,
    category_code: str,
    text: str,
    clauses: list[str],
    candidate: dict[str, Any],
    thresholds: SimilarityThresholds,
) -> dict[str, float | int] | None:
    """Score one document pair without requiring persisted policy metadata."""

    normalized_text = normalize_similarity_text(text)
    candidate_text = normalize_similarity_text(str(candidate.get("text") or ""))
    if not normalized_text or not candidate_text:
        return None

    title_score = Levenshtein.normalized_similarity(
        normalize_similarity_text(normalized_title or title),
        normalize_similarity_text(
            str(candidate.get("normalized_title") or candidate.get("title") or "")
        ),
    )
    same_title = bool(normalized_title) and normalized_title == candidate.get(
        "normalized_title"
    )
    same_category = category_code == candidate.get("category_code")
    length_ratio = min(len(normalized_text), len(candidate_text)) / max(
        len(normalized_text), len(candidate_text)
    )
    if not (
        same_title
        or title_score >= thresholds.title
        or (same_category and 0.75 <= length_ratio <= (1 / 0.75))
    ):
        return None

    edit_similarity = Levenshtein.normalized_similarity(
        normalized_text,
        candidate_text,
        score_cutoff=thresholds.edit,
    )
    left_grams = _ngrams(normalized_text, 5)
    right_grams = _ngrams(candidate_text, 5)
    intersection = len(left_grams & right_grams)
    union = len(left_grams | right_grams)
    jaccard = intersection / union if union else 1.0
    containment = (
        intersection / min(len(left_grams), len(right_grams))
        if left_grams and right_grams
        else 1.0
    )
    normalized_clauses = {
        normalize_similarity_text(clause)
        for clause in clauses
        if normalize_similarity_text(clause)
    }
    candidate_clauses = {
        normalize_similarity_text(clause)
        for clause in (candidate.get("clauses") or [])
        if normalize_similarity_text(clause)
    }
    clause_coverage = (
        len(normalized_clauses & candidate_clauses)
        / min(len(normalized_clauses), len(candidate_clauses))
        if normalized_clauses and candidate_clauses
        else 0.0
    )
    if not (
        edit_similarity >= thresholds.edit
        or jaccard >= thresholds.jaccard
        or (
            containment >= thresholds.containment
            and length_ratio >= thresholds.length_ratio
        )
        or clause_coverage >= thresholds.clause_coverage
    ):
        return None

    similarity_score = max(
        edit_similarity,
        jaccard,
        min(containment, length_ratio),
        clause_coverage,
    )
    estimated_change = round(
        100 * (1 - max(edit_similarity, jaccard, min(containment, length_ratio)))
    )
    return {
        "title_similarity": round(float(title_score), 6),
        "similarity_score": round(float(similarity_score), 6),
        "edit_similarity": round(float(edit_similarity), 6),
        "jaccard": round(jaccard, 6),
        "containment": round(containment, 6),
        "length_ratio": round(length_ratio, 6),
        "clause_coverage": round(clause_coverage, 6),
        "estimated_change_percent": max(0, min(100, estimated_change)),
    }


def similarity_report(
    *,
    title: str,
    normalized_title: str,
    category_code: str,
    text: str,
    clauses: list[str],
    candidates: list[dict[str, Any]],
    thresholds: SimilarityThresholds,
    limit: int = 3,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        metrics = similarity_metrics(
            title=title,
            normalized_title=normalized_title,
            category_code=category_code,
            text=text,
            clauses=clauses,
            candidate=candidate,
            thresholds=thresholds,
        )
        if metrics is None:
            continue

        matches.append(
            {
                "policy_id": str(candidate["policy_id"]),
                "title": candidate.get("title"),
                "current_version": candidate.get("version") or DEFAULT_VERSION,
                "proposed_version": format_policy_version(
                    int(
                        candidate.get("family_max_version_seq")
                        if candidate.get("family_max_version_seq") is not None
                        else candidate.get("version_seq") or 0
                    )
                    + 1
                ),
                "policy_status": candidate.get("status"),
                "candidate_file_name": candidate.get("candidate_file_name")
                or candidate.get("original_name"),
                **metrics,
            }
        )

    matches.sort(
        key=lambda item: (
            item["similarity_score"],
            item["title_similarity"],
        ),
        reverse=True,
    )
    selected = matches[: max(1, limit)]
    return {
        "status": SIMILARITY_DECISION_REQUIRED if selected else SIMILARITY_CLEAR,
        "basis": "max_20_percent_change",
        "candidates": selected,
        "allowed_decisions": (
            ["new_version", "separate", "discard"] if selected else []
        ),
    }


def _ngrams(value: str, size: int) -> set[str]:
    if len(value) < size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}
