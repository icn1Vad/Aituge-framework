from __future__ import annotations

from contract.application.idempotency import canonical_json
from contract.legal_evidence.models import LegalEvidence

_LEGAL_PROMPT_SAFETY_TOKENS = 512


def deterministic_token_upper_bound(value: str) -> int:
    """Return a tokenizer-independent upper bound for UTF-8/BPE prompts.

    A byte-level tokenizer cannot emit more tokens than input bytes.  The
    estimate is intentionally conservative so legal enrichment can never be
    the reason an otherwise valid legacy prompt crosses its hard limit.
    """

    return len(value.encode("utf-8"))


def remaining_legal_prompt_budget(
    *,
    baseline_prompt: str,
    system_prompt: str,
    hard_limit_tokens: int,
    safety_tokens: int = _LEGAL_PROMPT_SAFETY_TOKENS,
) -> int:
    baseline_upper_bound = deterministic_token_upper_bound(
        baseline_prompt
    ) + deterministic_token_upper_bound(system_prompt)
    return max(0, hard_limit_tokens - safety_tokens - baseline_upper_bound)


def legal_evidence_ids_for_check(
    evidence: list[LegalEvidence],
    check_code: str,
) -> list[str]:
    """Bind citations deterministically; the model never selects identifiers."""
    return sorted(
        {
            item.evidence_id
            for item in evidence
            if check_code in item.check_codes
        }
    )


def compact_legal_evidence_catalog(
    evidence: list[LegalEvidence],
    *,
    maximum_excerpt_characters: int = 900,
    maximum_catalog_tokens: int = 50_000,
) -> tuple[list[dict[str, object]], int]:
    """Compress excerpts within a conservative token budget.

    Bound citation identifiers are all retained or the entire legal catalog
    is omitted. Unbound retrieval candidates never enter the prompt. Partial
    bound identifier sets would make Finding provenance depend on arbitrary
    list order, so they are deliberately forbidden.
    """

    evidence = [item for item in evidence if item.check_codes]
    if not evidence or maximum_catalog_tokens <= 0:
        return [], 0
    bases: list[dict[str, object]] = []
    for item in evidence:
        bases.append(
            {
                "evidence_id": item.evidence_id,
                "check_codes": item.check_codes,
                "statute": item.unit.title[:160],
                "article": item.unit.article_no,
                "content_hash": item.unit.content_hash,
                "jurisdiction": item.unit.jurisdiction,
                "validity_status": item.unit.validity_status,
                "verification_status": item.unit.metadata_verification_status,
            }
        )
    if deterministic_token_upper_bound(canonical_json(bases)) > maximum_catalog_tokens:
        return [], 0

    def build(per_item_excerpt: int) -> list[dict[str, object]]:
        catalog: list[dict[str, object]] = []
        for item, base in zip(evidence, bases, strict=True):
            text = item.unit.content
            center = 0
            for concept in item.matched_concepts:
                position = text.find(concept)
                if position >= 0:
                    center = position
                    break
            if per_item_excerpt <= 0:
                excerpt = ""
            elif len(text) <= per_item_excerpt:
                excerpt = text
            else:
                half = per_item_excerpt // 2
                start = max(0, min(center - half, len(text) - per_item_excerpt))
                excerpt = text[start : start + per_item_excerpt]
            catalog.append(
                {
                    **base,
                    "content_excerpt": excerpt,
                    "content_truncated": len(excerpt) < len(text),
                }
            )
        return catalog

    low = 0
    high = maximum_excerpt_characters
    accepted = build(0)
    if (
        deterministic_token_upper_bound(canonical_json(accepted))
        > maximum_catalog_tokens
    ):
        return [], 0
    while low <= high:
        candidate_size = (low + high) // 2
        candidate = build(candidate_size)
        if (
            deterministic_token_upper_bound(canonical_json(candidate))
            <= maximum_catalog_tokens
        ):
            accepted = candidate
            low = candidate_size + 1
        else:
            high = candidate_size - 1
    estimated_tokens = deterministic_token_upper_bound(canonical_json(accepted))
    return accepted, estimated_tokens
