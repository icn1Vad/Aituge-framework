from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from contract.legal_evidence.models import LegalRetrievalUnit

_ARTICLE_REF = re.compile(r"(?:本法|本条例|本规定|本办法)?(第[零〇一二三四五六七八九十百千万0-9]+条)")
_NAMED_INSTRUMENT_REF = re.compile(r"《\s*([^》]{1,200}?)\s*》")
_NAMED_ARTICLE_REF = re.compile(
    r"《\s*[^》]{1,200}?\s*》\s*(第[零〇一二三四五六七八九十百千万0-9]+条)"
)
_RELATION_HINTS: tuple[
    tuple[Literal[
        "BASED_ON", "IMPLEMENTS", "INTERPRETS", "AMENDS", "REPEALS",
        "REPLACES", "SUPPLEMENTS",
    ], tuple[str, ...]],
    ...,
] = (
    ("REPEALS", ("废止", "停止施行")),
    ("REPLACES", ("代替", "替代")),
    ("AMENDS", ("修改", "修订")),
    ("INTERPRETS", ("解释", "释义")),
    ("IMPLEMENTS", ("实施", "贯彻", "施行")),
    ("BASED_ON", ("根据", "依据", "依照", "制定依据")),
    ("SUPPLEMENTS", ("补充", "未尽事宜")),
)


def normalize_instrument_alias(value: str) -> str:
    """Normalize only typography; never use fuzzy title matching for graph edges."""
    return re.sub(r"[\s《》〈〉]", "", value).strip()


@dataclass(frozen=True, slots=True)
class NamedInstrumentReference:
    title: str
    normalized_title: str
    relation_type: Literal[
        "CITES", "BASED_ON", "IMPLEMENTS", "INTERPRETS", "AMENDS",
        "REPEALS", "REPLACES", "SUPPLEMENTS",
    ]
    evidence_text: str
    target_article_no: str | None = None
    verification_status: Literal["AUTO_VERIFIED", "CANDIDATE"] = "AUTO_VERIFIED"


def extract_named_instrument_references(content: str) -> list[NamedInstrumentReference]:
    """Extract explicit ``《法规名》`` references with a short evidence window.

    This is deliberately deterministic. Semantic similarity is not persisted as
    a verified graph edge.
    """
    references: list[NamedInstrumentReference] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _NAMED_INSTRUMENT_REF.finditer(content):
        title = re.sub(r"\s+", "", match.group(1)).strip()
        normalized = normalize_instrument_alias(title)
        if not normalized:
            continue
        start = max(0, match.start() - 32)
        end = min(len(content), match.end() + 32)
        context = re.sub(r"\s+", " ", content[start:end]).strip()
        suffix = content[match.end() : min(len(content), match.end() + 40)]
        article_match = re.match(
            r"\s*(第[零〇一二三四五六七八九十百千万0-9]+条)", suffix
        )
        relation_type = "CITES"
        local_prefix = content[max(0, match.start() - 20) : match.start()]
        local_suffix = content[match.end() : min(len(content), match.end() + 20)]
        local_relation_context = local_prefix + "《法规》" + local_suffix
        verification_status: Literal["AUTO_VERIFIED", "CANDIDATE"] = "AUTO_VERIFIED"
        for candidate, hints in _RELATION_HINTS:
            matched_hint = next(
                (
                    hint
                    for hint in hints
                    if re.search(
                        rf"(?:{re.escape(hint)}.{{0,10}}《法规》|《法规》.{{0,10}}{re.escape(hint)})",
                        local_relation_context,
                    )
                ),
                None,
            )
            if matched_hint:
                if re.search(
                    rf"(?:不|未|并非|不得).{{0,3}}{re.escape(matched_hint)}",
                    local_relation_context,
                ):
                    break
                relation_type = candidate
                if candidate not in {"BASED_ON", "IMPLEMENTS"}:
                    # The title link is exact, but legal effects such as repeal,
                    # amendment and replacement require later verification.
                    verification_status = "CANDIDATE"
                break
        identity = (normalized, relation_type, context)
        if identity in seen:
            continue
        seen.add(identity)
        references.append(
            NamedInstrumentReference(
                title=title,
                normalized_title=normalized,
                relation_type=relation_type,
                evidence_text=context[:320],
                target_article_no=(article_match.group(1) if article_match else None),
                verification_status=verification_status,
            )
        )
    return references


def instrument_aliases(row: dict[str, Any]) -> set[str]:
    values: list[str] = []
    for key in ("title", "normalized_title"):
        value = str(row.get(key) or "").strip()
        if value:
            values.append(value)
    try:
        raw_titles = json.loads(row.get("raw_titles_json") or "[]")
    except (TypeError, ValueError):
        raw_titles = []
    if isinstance(raw_titles, list):
        values.extend(str(value) for value in raw_titles if str(value).strip())
    return {
        normalized
        for value in values
        if (normalized := normalize_instrument_alias(value))
    }


def unique_instrument_aliases(
    rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, str], set[str]]:
    candidates: dict[str, set[str]] = {}
    for row in rows:
        instrument_key = str(row["instrument_key"])
        for alias in instrument_aliases(row):
            candidates.setdefault(alias, set()).add(instrument_key)
    unique = {
        alias: next(iter(keys)) for alias, keys in candidates.items() if len(keys) == 1
    }
    ambiguous = {alias for alias, keys in candidates.items() if len(keys) > 1}
    return unique, ambiguous


def extract_internal_references(content: str) -> list[str]:
    """Extract explicit article references without inferring semantic edges."""
    external_article_spans = [
        match.span(1) for match in _NAMED_ARTICLE_REF.finditer(content)
    ]
    values: list[str] = []
    for match in _ARTICLE_REF.finditer(content):
        span = match.span(1)
        if any(span[0] >= start and span[1] <= end for start, end in external_article_spans):
            continue
        values.append(match.group(1))
    return list(dict.fromkeys(values))


def _json_names(raw: Any) -> str | None:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list):
        return None
    names = [str(item).strip() for item in values if str(item).strip()]
    return "、".join(names) or None


@dataclass(slots=True)
class _ArticleBuffer:
    first: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)


class LegalArticleAssembler:
    """Stream MySQL nodes into complete-article retrieval units.

    ARTICLE and all following descendants are kept together. This avoids the
    common failure where a bare ``第X条`` heading is indexed separately from its
    paragraphs. Preamble is retained as one standalone unit; chapter/section
    headings only contribute through the node path.
    """

    def __init__(self) -> None:
        self._buffer: _ArticleBuffer | None = None

    def feed(self, row: dict[str, Any]) -> list[LegalRetrievalUnit]:
        emitted: list[LegalRetrievalUnit] = []
        node_type = str(row.get("node_type") or "").upper()
        if self._buffer is not None and not self._belongs_to_current(row):
            emitted.append(self._emit())
        if node_type == "ARTICLE":
            if self._buffer is not None:
                emitted.append(self._emit())
            self._buffer = _ArticleBuffer(first=row, rows=[row])
        elif node_type == "PREAMBLE":
            if self._buffer is not None:
                emitted.append(self._emit())
            self._buffer = _ArticleBuffer(first=row, rows=[row])
            emitted.append(self._emit())
        elif self._buffer is not None and self._belongs_to_current(row):
            self._buffer.rows.append(row)
        return emitted

    def finish(self) -> list[LegalRetrievalUnit]:
        return [self._emit()] if self._buffer is not None else []

    def _belongs_to_current(self, row: dict[str, Any]) -> bool:
        if self._buffer is None:
            return False
        first = self._buffer.first
        if row.get("version_id") != first.get("version_id"):
            return False
        root_path = str(first.get("path") or "")
        path = str(row.get("path") or "")
        return bool(root_path and path.startswith(root_path + "/"))

    def _emit(self) -> LegalRetrievalUnit:
        if self._buffer is None:
            raise RuntimeError("No legal article is buffered")
        first = self._buffer.first
        rows = self._buffer.rows
        self._buffer = None
        parts: list[str] = []
        for row in rows:
            text = str(row.get("content_plain") or "").strip()
            if text and (not parts or text != parts[-1]):
                parts.append(text)
        content = "\n".join(parts).strip()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        identity = "|".join(
            (
                str(first.get("release_id") or ""),
                str(first.get("version_id") or ""),
                str(first.get("node_id") or ""),
                digest,
            )
        )
        return LegalRetrievalUnit(
            unit_id="legal-unit-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
            release_id=str(first["release_id"]),
            instrument_id=str(first["instrument_key"]),
            version_id=str(first["version_id"]),
            source_node_ids=[str(row["node_id"]) for row in rows],
            title=str(first["title"]),
            article_no=(str(first["node_number"]) if first.get("node_number") else None),
            heading_path=[part for part in str(first.get("path") or "").split("/") if part],
            content=content,
            jurisdiction=(
                str(first["jurisdiction_code"]) if first.get("jurisdiction_code") else None
            ),
            authority_level=(
                str(first["category_root"]) if first.get("category_root") else None
            ),
            issuing_authority=_json_names(first.get("issuing_authority_names_json")),
            # Source release explicitly disables date and status extraction.
            # Never interpret a filename or free-form title as an effective date.
            effective_from=None,
            effective_to=None,
            validity_status="UNKNOWN",
            metadata_verification_status="UNVERIFIED",
            official_source_url=(str(first["source_url"]) if first.get("source_url") else None),
            content_hash=digest,
            sequence=int(first.get("sequence") or 0),
        )
