from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from contract.parser.models import ParsedContractBlock


TokenEstimator = Callable[[str], int]

_CHINESE_NUMBER = "〇零一二三四五六七八九十百千万两0-9０-９"
_CHAPTER_RE = re.compile(rf"^\s*第\s*[{_CHINESE_NUMBER}]+\s*(?:编|章|篇)")
_SECTION_RE = re.compile(rf"^\s*第\s*[{_CHINESE_NUMBER}]+\s*节")
_ARTICLE_RE = re.compile(rf"^\s*(第\s*[{_CHINESE_NUMBER}]+\s*条)")
_SUBCLAUSE_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)+[.、]?|[（(][一二三四五六七八九十\d]+[）)]|\d+[）)])"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？；.!?;]\s*|\n+")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


class WindowBuildError(ValueError):
    """Raised when deterministic section/window invariants cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class SourceSlice:
    block_id: str
    block_no: int
    block_type: str
    text: str
    page_number: int | None
    block_char_start: int
    block_char_end: int
    heading_path: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_block(cls, block: ParsedContractBlock) -> "SourceSlice":
        return cls(
            block_id=block.block_id,
            block_no=block.block_no,
            block_type=block.block_type,
            text=block.text,
            page_number=block.page_number,
            block_char_start=0,
            block_char_end=len(block.text),
            heading_path=tuple(block.heading_path),
            metadata=dict(block.metadata),
        )


@dataclass(frozen=True, slots=True)
class SectionUnit:
    section_id: str
    sequence_no: int
    section_type: str
    title: str
    clause_no: str | None
    heading_path: tuple[str, ...]
    parent_section_id: str | None
    major_section_id: str | None
    subclause_block_ids: tuple[str, ...]
    source_slices: tuple[SourceSlice, ...]

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.block_id for item in self.source_slices))


@dataclass(frozen=True, slots=True)
class WindowOffset:
    rendered_start: int
    rendered_end: int
    block_id: str
    block_no: int
    block_char_start: int
    block_char_end: int
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class SectionWindow:
    window_id: str
    sequence_no: int
    section_ids: tuple[str, ...]
    heading_path: tuple[str, ...]
    clause_nos: tuple[str, ...]
    primary_block_ids: tuple[str, ...]
    source_text: str
    context_text: str
    offset_map: tuple[WindowOffset, ...]
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class CoverageReport:
    expected_block_count: int
    covered_block_count: int
    split_block_ids: tuple[str, ...]
    missing_block_ids: tuple[str, ...]
    overlap_block_ids: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.missing_block_ids and not self.overlap_block_ids


def estimate_model_tokens(text: str) -> int:
    """Return a conservative dependency-free estimate for mixed Chinese/Latin text."""

    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    other_count = len(text) - cjk_count
    return max(1, cjk_count + math.ceil(other_count / 4))


def build_section_units(blocks: Sequence[ParsedContractBlock]) -> list[SectionUnit]:
    content = [item for item in blocks if item.block_type != "footer" and item.text]
    if not content:
        raise WindowBuildError("Contract windowing requires at least one source block")

    grouped: list[tuple[str, list[ParsedContractBlock]]] = []
    current_type = "PREAMBLE"
    current: list[ParsedContractBlock] = []
    for block in content:
        boundary_type = _section_boundary_type(block)
        if boundary_type is not None and current:
            grouped.append((current_type, current))
            current = []
        if boundary_type is not None:
            current_type = boundary_type
        current.append(block)
    if current:
        grouped.append((current_type, current))

    units = [_unit_from_blocks(index, section_type, items) for index, (section_type, items) in enumerate(grouped, 1)]
    return _assign_hierarchy(units)


def build_section_windows(
    sections: Sequence[SectionUnit],
    *,
    soft_token_limit: int = 4_000,
    hard_token_limit: int = 6_000,
    context_lines: Iterable[str] = (),
    token_estimator: TokenEstimator = estimate_model_tokens,
) -> list[SectionWindow]:
    if soft_token_limit <= 0 or hard_token_limit <= 0:
        raise WindowBuildError("Window token limits must be positive")
    if soft_token_limit > hard_token_limit:
        raise WindowBuildError("Window soft token limit cannot exceed hard limit")
    if not sections:
        raise WindowBuildError("Contract windowing requires at least one section")

    fragments: list[tuple[SectionUnit, tuple[SourceSlice, ...]]] = []
    for section in sections:
        fragments.extend(
            (section, tuple(items))
            for items in _split_section(section, hard_token_limit, token_estimator)
        )

    context_text = "\n".join(item.strip() for item in context_lines if item.strip())
    packed: list[list[tuple[SectionUnit, tuple[SourceSlice, ...]]]] = []
    current: list[tuple[SectionUnit, tuple[SourceSlice, ...]]] = []
    for fragment in fragments:
        candidate = [*current, fragment]
        candidate_tokens = token_estimator(_render_slices(_flatten_slices(candidate))[0])
        crosses_major_boundary = bool(current) and not _can_share_window(current[-1][0], fragment[0])
        if current and (candidate_tokens > soft_token_limit or crosses_major_boundary):
            packed.append(current)
            current = [fragment]
        else:
            current = candidate
    if current:
        packed.append(current)

    windows = [
        _window_from_fragments(
            index,
            fragments_in_window,
            context_text=context_text,
            token_estimator=token_estimator,
            hard_token_limit=hard_token_limit,
        )
        for index, fragments_in_window in enumerate(packed, 1)
    ]
    return windows


def validate_window_coverage(
    blocks: Sequence[ParsedContractBlock],
    windows: Sequence[SectionWindow],
) -> CoverageReport:
    expected = {
        item.block_id: item
        for item in blocks
        if item.block_type != "footer" and item.text
    }
    positions: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for window in windows:
        for offset in window.offset_map:
            rendered = window.source_text[offset.rendered_start : offset.rendered_end]
            block = expected.get(offset.block_id)
            if block is None:
                raise WindowBuildError(f"Window references unknown block '{offset.block_id}'")
            source = block.text[offset.block_char_start : offset.block_char_end]
            if rendered != source:
                raise WindowBuildError(f"Window offset text does not match block '{offset.block_id}'")
            positions[offset.block_id].append((offset.block_char_start, offset.block_char_end))

    missing: list[str] = []
    overlaps: list[str] = []
    split: list[str] = []
    for block_id, block in expected.items():
        spans = sorted(positions.get(block_id, []))
        if not spans:
            missing.append(block_id)
            continue
        if len(spans) > 1:
            split.append(block_id)
        cursor = 0
        valid = True
        for start, end in spans:
            if start != cursor or end <= start:
                valid = False
                break
            cursor = end
        if not valid or cursor != len(block.text):
            overlaps.append(block_id)

    report = CoverageReport(
        expected_block_count=len(expected),
        covered_block_count=len(expected) - len(missing),
        split_block_ids=tuple(split),
        missing_block_ids=tuple(missing),
        overlap_block_ids=tuple(overlaps),
    )
    if not report.valid:
        raise WindowBuildError(
            "Window coverage is invalid: "
            f"missing={list(report.missing_block_ids)}, invalid={list(report.overlap_block_ids)}"
        )
    return report


def _section_boundary_type(block: ParsedContractBlock) -> str | None:
    value = block.text.strip()
    if _CHAPTER_RE.match(value):
        return "CHAPTER"
    if _SECTION_RE.match(value):
        return "SECTION"
    if block.block_type == "article" or _ARTICLE_RE.match(value):
        return "ARTICLE"
    if block.block_type == "heading":
        return "HEADING"
    return None


def _unit_from_blocks(
    sequence_no: int,
    section_type: str,
    blocks: Sequence[ParsedContractBlock],
) -> SectionUnit:
    first = blocks[0]
    title = first.text if section_type != "PREAMBLE" else "合同序言"
    article = _ARTICLE_RE.match(first.text)
    clause_no = re.sub(r"\s+", "", article.group(1)) if article else None
    section_id = _stable_id(
        "section",
        str(sequence_no),
        section_type,
        first.block_id,
        blocks[-1].block_id,
    )
    return SectionUnit(
        section_id=section_id,
        sequence_no=sequence_no,
        section_type=section_type,
        title=title,
        clause_no=clause_no,
        heading_path=tuple(first.heading_path),
        parent_section_id=None,
        major_section_id=None,
        subclause_block_ids=tuple(
            item.block_id for item in blocks if _SUBCLAUSE_RE.match(item.text.strip())
        ),
        source_slices=tuple(SourceSlice.from_block(item) for item in blocks),
    )


def _assign_hierarchy(units: Sequence[SectionUnit]) -> list[SectionUnit]:
    active_chapter: str | None = None
    active_section: str | None = None
    result: list[SectionUnit] = []
    for unit in units:
        parent: str | None = None
        major: str | None = active_chapter
        if unit.section_type == "CHAPTER":
            active_chapter = unit.section_id
            active_section = None
            major = unit.section_id
        elif unit.section_type == "SECTION":
            parent = active_chapter
            active_section = unit.section_id
        elif unit.section_type in {"ARTICLE", "HEADING"}:
            parent = active_section or active_chapter
        result.append(
            SectionUnit(
                section_id=unit.section_id,
                sequence_no=unit.sequence_no,
                section_type=unit.section_type,
                title=unit.title,
                clause_no=unit.clause_no,
                heading_path=unit.heading_path,
                parent_section_id=parent,
                major_section_id=major,
                subclause_block_ids=unit.subclause_block_ids,
                source_slices=unit.source_slices,
            )
        )
    return result


def _split_section(
    section: SectionUnit,
    hard_limit: int,
    estimator: TokenEstimator,
) -> list[list[SourceSlice]]:
    rendered, _ = _render_slices(section.source_slices)
    if estimator(rendered) <= hard_limit:
        return [list(section.source_slices)]

    groups = _atomic_groups(section.source_slices)
    fragments: list[list[SourceSlice]] = []
    current: list[SourceSlice] = []
    for group in groups:
        normalized_group: list[SourceSlice] = []
        group_text, _ = _render_slices(group)
        if estimator(group_text) > hard_limit:
            for item in group:
                normalized_group.extend(_split_source_slice(item, hard_limit, estimator))
        else:
            normalized_group.extend(group)
        for item in normalized_group:
            candidate = [*current, item]
            candidate_text, _ = _render_slices(candidate)
            if current and estimator(candidate_text) > hard_limit:
                fragments.append(current)
                current = [item]
            else:
                current = candidate
    if current:
        fragments.append(current)
    return fragments


def _atomic_groups(slices: Sequence[SourceSlice]) -> list[list[SourceSlice]]:
    groups: list[list[SourceSlice]] = []
    current_table: list[SourceSlice] = []
    current_table_no: object | None = None
    for item in slices:
        table_no = item.metadata.get("table_no") if item.block_type == "table_row" else None
        if table_no is not None:
            if current_table and table_no != current_table_no:
                groups.append(current_table)
                current_table = []
            current_table_no = table_no
            current_table.append(item)
            continue
        if current_table:
            groups.append(current_table)
            current_table = []
            current_table_no = None
        groups.append([item])
    if current_table:
        groups.append(current_table)
    return groups


def _split_source_slice(
    item: SourceSlice,
    hard_limit: int,
    estimator: TokenEstimator,
) -> list[SourceSlice]:
    ranges = _split_text_ranges(item.text, hard_limit, estimator)
    return [
        SourceSlice(
            block_id=item.block_id,
            block_no=item.block_no,
            block_type=item.block_type,
            text=item.text[start:end],
            page_number=item.page_number,
            block_char_start=item.block_char_start + start,
            block_char_end=item.block_char_start + end,
            heading_path=item.heading_path,
            metadata=dict(item.metadata),
        )
        for start, end in ranges
    ]


def _split_text_ranges(
    text: str,
    hard_limit: int,
    estimator: TokenEstimator,
) -> list[tuple[int, int]]:
    if estimator(text) <= hard_limit:
        return [(0, len(text))]
    boundaries = [match.end() for match in _SENTENCE_BOUNDARY_RE.finditer(text)]
    boundaries.append(len(text))
    result: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best = start + 1
        while low <= high:
            middle = (low + high) // 2
            if estimator(text[start:middle]) <= hard_limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        preferred = max((item for item in boundaries if start < item <= best), default=best)
        end = preferred if preferred > start else best
        result.append((start, end))
        start = end
    return result


def _can_share_window(previous: SectionUnit, current: SectionUnit) -> bool:
    if previous.section_type == "CHAPTER" or current.section_type == "CHAPTER":
        return previous.major_section_id == current.major_section_id
    if previous.major_section_id and current.major_section_id:
        return previous.major_section_id == current.major_section_id
    return True


def _window_from_fragments(
    sequence_no: int,
    fragments: Sequence[tuple[SectionUnit, tuple[SourceSlice, ...]]],
    *,
    context_text: str,
    token_estimator: TokenEstimator,
    hard_token_limit: int,
) -> SectionWindow:
    slices = _flatten_slices(fragments)
    source_text, offsets = _render_slices(slices)
    estimated_tokens = token_estimator(source_text)
    if estimated_tokens > hard_token_limit:
        raise WindowBuildError(
            f"Window {sequence_no} exceeds hard token limit: {estimated_tokens}>{hard_token_limit}"
        )
    section_ids = tuple(dict.fromkeys(item[0].section_id for item in fragments))
    clause_nos = tuple(
        dict.fromkeys(item[0].clause_no for item in fragments if item[0].clause_no)
    )
    heading_path = next((item[0].heading_path for item in fragments if item[0].heading_path), ())
    primary_block_ids = tuple(dict.fromkeys(item.block_id for item in slices))
    window_id = _stable_id(
        "window",
        str(sequence_no),
        *section_ids,
        *(f"{item.block_id}:{item.block_char_start}:{item.block_char_end}" for item in slices),
    )
    return SectionWindow(
        window_id=window_id,
        sequence_no=sequence_no,
        section_ids=section_ids,
        heading_path=heading_path,
        clause_nos=clause_nos,
        primary_block_ids=primary_block_ids,
        source_text=source_text,
        context_text=context_text,
        offset_map=offsets,
        estimated_tokens=estimated_tokens,
    )


def _flatten_slices(
    fragments: Sequence[tuple[SectionUnit, tuple[SourceSlice, ...]]],
) -> tuple[SourceSlice, ...]:
    return tuple(item for _, slices in fragments for item in slices)


def _render_slices(slices: Sequence[SourceSlice]) -> tuple[str, tuple[WindowOffset, ...]]:
    parts: list[str] = []
    offsets: list[WindowOffset] = []
    cursor = 0
    for item in slices:
        if parts:
            parts.append("\n\n")
            cursor += 2
        start = cursor
        parts.append(item.text)
        cursor += len(item.text)
        offsets.append(
            WindowOffset(
                rendered_start=start,
                rendered_end=cursor,
                block_id=item.block_id,
                block_no=item.block_no,
                block_char_start=item.block_char_start,
                block_char_end=item.block_char_end,
                page_number=item.page_number,
            )
        )
    return "".join(parts), tuple(offsets)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
