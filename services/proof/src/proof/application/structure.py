from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import replace
from typing import Any

from proof.domain import DocumentBlock, RetrievalUnit, StructureExtractionResult
from proof.domain.numbering import MarkerKind, NumberingMarker, detect_numbering_marker, is_toc_entry
from proof.errors import ProofError


STRUCTURE_ENGINE_VERSION = "policy-structure-v1"
ARTICLE_PROFILE = "article"
DECIMAL_PROFILE = "decimal_outline"
CHINESE_PROFILE = "chinese_outline"
MIXED_PROFILE = "mixed"


def extract_policy_structure(blocks: list[DocumentBlock]) -> StructureExtractionResult:
    markers = _markers_for(blocks)
    article_indexes = [index for index, marker in markers.items() if marker.kind == MarkerKind.ARTICLE]
    extracted: list[tuple[list[RetrievalUnit], dict[str, Any]]] = []

    if article_indexes:
        first_article = article_indexes[0]
        last_article = article_indexes[-1]
        prefix_profile = _detect_outline_profile(blocks, markers, 0, first_article, allow_flat=False)
        if prefix_profile:
            extracted.append(_extract_outline_region(blocks, markers, 0, first_article, prefix_profile))

        article_end = len(blocks)
        suffix: tuple[int, str] | None = None
        for index in range(last_article + 1, len(blocks)):
            marker = markers.get(index)
            if marker is None or marker.kind != MarkerKind.APPENDIX:
                continue
            profile = _detect_outline_profile(blocks, markers, index + 1, len(blocks), allow_flat=True)
            if profile:
                suffix = (index, profile)
                article_end = index
                break

        extracted.append(_extract_article_region(blocks, markers, first_article, article_end))
        if suffix:
            extracted.append(_extract_outline_region(blocks, markers, suffix[0], len(blocks), suffix[1]))
    else:
        profile = _detect_outline_profile(blocks, markers, 0, len(blocks), allow_flat=True)
        if profile:
            extracted.append(_extract_outline_region(blocks, markers, 0, len(blocks), profile))

    extracted = [(units, region) for units, region in extracted if units]
    if not extracted:
        raise ProofError(
            "no_clauses_found",
            "No supported policy numbering structure was found.",
            status_code=422,
            details={"supported_profiles": [ARTICLE_PROFILE, DECIMAL_PROFILE, CHINESE_PROFILE]},
        )

    units: list[RetrievalUnit] = []
    regions: list[dict[str, Any]] = []
    for region_units, region in extracted:
        region["unit_start_ordinal"] = len(units) + 1
        units.extend(region_units)
        region["unit_end_ordinal"] = len(units)
        region["unit_count"] = len(region_units)
        regions.append(region)
    units = [replace(unit, clause_ordinal=index) for index, unit in enumerate(units, start=1)]

    profiles = list(dict.fromkeys(region["profile"] for region in regions))
    profile = profiles[0] if len(profiles) == 1 else MIXED_PROFILE
    warnings = _structure_warnings(units, markers, profiles)
    assigned_ids = {block_id for unit in units for block_id in unit.source_block_ids}
    context_texts = {heading for unit in units for heading in unit.heading_path}
    region_ranges = [
        (region["block_start_ordinal"], region["block_end_ordinal"])
        for region in regions
        if region["block_start_ordinal"] is not None and region["block_end_ordinal"] is not None
    ]
    content_blocks = [
        block
        for block in blocks
        if block.block_type not in {"heading", "footer"}
        and not is_toc_entry(block.text)
        and any(first <= block.ordinal <= last for first, last in region_ranges)
    ]
    handled_count = sum(block.id in assigned_ids or block.text in context_texts for block in content_blocks)
    unassigned_markers = [
        blocks[index].ordinal
        for index, marker in markers.items()
        if marker.kind
        in {
            MarkerKind.ARTICLE,
            MarkerKind.DECIMAL,
            MarkerKind.ARABIC_HEADING,
            MarkerKind.CHINESE_HEADING,
            MarkerKind.PAREN_CHINESE,
        }
        and blocks[index].id not in assigned_ids
        and blocks[index].text not in context_texts
        and blocks[index].block_type not in {"heading", "footer"}
        and not is_toc_entry(blocks[index].text)
        and any(first <= blocks[index].ordinal <= last for first, last in region_ranges)
    ]
    diagnostics = {
        "engine_version": STRUCTURE_ENGINE_VERSION,
        "profile": profile,
        "profiles_detected": profiles,
        "regions": regions,
        "block_count": len(blocks),
        "content_block_count": len(content_blocks),
        "retrieval_block_count": sum(block.id in assigned_ids for block in content_blocks),
        "context_block_count": sum(block.text in context_texts for block in content_blocks),
        "content_coverage_rate": round(handled_count / len(content_blocks), 4) if content_blocks else 0.0,
        "ignored_preamble_block_count": sum(
            block.block_type not in {"heading", "footer"}
            and not is_toc_entry(block.text)
            and not any(first <= block.ordinal <= last for first, last in region_ranges)
            for block in blocks
        ),
        "indentation_evidence_block_count": sum(_block_indent(block) not in {None, 0} for block in blocks),
        "unassigned_numbering_block_ordinals": unassigned_markers,
        "warning_count": len(warnings),
    }
    if unassigned_markers:
        warnings.append(
            {
                "code": "unassigned_numbering",
                "severity": "warning",
                "message": f"{len(unassigned_markers)} 个编号 block 未归入 retrieval unit。",
                "block_ordinals": unassigned_markers[:50],
            }
        )
        diagnostics["warning_count"] = len(warnings)
    return StructureExtractionResult(units, profile, profiles, regions, warnings, diagnostics)


def _markers_for(blocks: list[DocumentBlock]) -> dict[int, NumberingMarker]:
    return {
        index: marker
        for index, block in enumerate(blocks)
        if not is_toc_entry(block.text) and (marker := detect_numbering_marker(block.text)) is not None
    }


def _detect_outline_profile(
    blocks: list[DocumentBlock],
    markers: dict[int, NumberingMarker],
    start: int,
    end: int,
    *,
    allow_flat: bool,
) -> str | None:
    values = [marker for index, marker in markers.items() if start <= index < end]
    decimal_count = sum(marker.kind == MarkerKind.DECIMAL for marker in values)
    arabic_count = sum(marker.kind == MarkerKind.ARABIC_HEADING for marker in values)
    arabic_item_count = sum(marker.kind == MarkerKind.ARABIC_ITEM for marker in values)
    chinese_count = sum(marker.kind == MarkerKind.CHINESE_HEADING for marker in values)
    paren_chinese_count = sum(marker.kind == MarkerKind.PAREN_CHINESE for marker in values)

    decimal_strong = (
        decimal_count >= (2 if allow_flat else 5)
        or (allow_flat and (arabic_count >= 2 or arabic_item_count >= 2))
        or (not allow_flat and decimal_count >= 2 and arabic_count >= 2)
    )
    chinese_strong = allow_flat and (
        chinese_count >= 2 or (chinese_count >= 1 and paren_chinese_count >= 2)
    )
    if decimal_strong and chinese_strong:
        decimal_score = decimal_count * 3 + arabic_count + arabic_item_count
        chinese_score = chinese_count * 3 + paren_chinese_count
        return DECIMAL_PROFILE if decimal_score >= chinese_score else CHINESE_PROFILE
    if decimal_strong:
        return DECIMAL_PROFILE
    if chinese_strong:
        return CHINESE_PROFILE
    return None


def _extract_article_region(
    blocks: list[DocumentBlock],
    markers: dict[int, NumberingMarker],
    start: int,
    end: int,
) -> tuple[list[RetrievalUnit], dict[str, Any]]:
    units: list[RetrievalUnit] = []
    active: list[DocumentBlock] = []
    label = ""

    def finish() -> None:
        nonlocal active, label
        if active:
            units.append(_build_unit(active, label, len(units) + 1, ARTICLE_PROFILE))
        active = []
        label = ""

    for index in range(start, end):
        block = blocks[index]
        marker = markers.get(index)
        if marker and marker.kind == MarkerKind.ARTICLE:
            finish()
            label = marker.raw
            active = [block]
            continue
        if not active or block.block_type in {"heading", "footer"}:
            continue
        active.append(block)
    finish()
    return units, _region_payload(blocks, start, end, ARTICLE_PROFILE, len(units), 1)


def _extract_outline_region(
    blocks: list[DocumentBlock],
    markers: dict[int, NumberingMarker],
    start: int,
    end: int,
    profile: str,
) -> tuple[list[RetrievalUnit], dict[str, Any]]:
    if profile == DECIMAL_PROFILE:
        units, depth, marker_count = _extract_decimal_units(blocks, markers, start, end)
    else:
        units, depth, marker_count = _extract_chinese_units(blocks, markers, start, end)
    return units, _region_payload(blocks, start, end, profile, marker_count, depth)


def _extract_decimal_units(
    blocks: list[DocumentBlock],
    markers: dict[int, NumberingMarker],
    start: int,
    end: int,
) -> tuple[list[RetrievalUnit], int, int]:
    entries = [
        (index, marker)
        for index, marker in markers.items()
        if start <= index < end and marker.kind in {MarkerKind.ARABIC_HEADING, MarkerKind.DECIMAL}
    ]
    if sum(marker.kind == MarkerKind.DECIMAL for _, marker in entries) < 2 and sum(
        marker.kind == MarkerKind.ARABIC_HEADING for _, marker in entries
    ) < 2:
        entries.extend(
            (index, marker)
            for index, marker in markers.items()
            if start <= index < end and marker.kind == MarkerKind.ARABIC_ITEM
        )
        entries.sort(key=lambda item: item[0])
    top_entries = _main_numeric_top_entries(
        _peer_indent_entries(
            [(index, marker) for index, marker in entries if marker.depth == 1],
            blocks,
        )
    )
    units: list[RetrievalUnit] = []

    if not top_entries:
        anchor_depth = min(marker.depth for _, marker in entries)
        boundaries = [(index, marker) for index, marker in entries if marker.depth == anchor_depth]
        for position, (unit_start, marker) in enumerate(boundaries):
            unit_end = boundaries[position + 1][0] if position + 1 < len(boundaries) else end
            unit_blocks = _retrieval_blocks(blocks, unit_start, unit_end)
            if unit_blocks:
                units.append(_build_unit(unit_blocks, marker.raw, len(units) + 1, DECIMAL_PROFILE))
        return units, anchor_depth, len(entries)

    anchor_depth = 2 if sum(marker.depth == 2 for _, marker in entries) >= 2 else 1
    for top_position, (top_index, top_marker) in enumerate(top_entries):
        section_end = top_entries[top_position + 1][0] if top_position + 1 < len(top_entries) else end
        child_entries = [
            (index, marker)
            for index, marker in entries
            if top_index < index < section_end
            and marker.kind == MarkerKind.DECIMAL
            and marker.depth == anchor_depth
            and marker.path[:1] == top_marker.path[:1]
        ]
        if child_entries:
            for child_position, (unit_start, marker) in enumerate(child_entries):
                unit_end = child_entries[child_position + 1][0] if child_position + 1 < len(child_entries) else section_end
                unit_blocks = _retrieval_blocks(blocks, unit_start, unit_end)
                if unit_blocks:
                    heading = _heading_context(blocks, start, top_index) + [blocks[top_index].text]
                    units.append(
                        _build_unit(unit_blocks, marker.raw, len(units) + 1, DECIMAL_PROFILE, heading)
                    )
        else:
            unit_blocks = _retrieval_blocks(blocks, top_index, section_end)
            if unit_blocks:
                units.append(
                    _build_unit(
                        unit_blocks,
                        top_marker.raw,
                        len(units) + 1,
                        DECIMAL_PROFILE,
                        _heading_context(blocks, start, top_index),
                    )
                )
    return units, anchor_depth, len(entries)


def _main_numeric_top_entries(
    entries: list[tuple[int, NumberingMarker]],
) -> list[tuple[int, NumberingMarker]]:
    """Keep the main increasing chapter run; restarted inline lists stay nested."""
    if not entries:
        return []
    start_position = 0
    for position, (_, marker) in enumerate(entries):
        if marker.path == (1,) and any(later.path == (2,) for _, later in entries[position + 1 :]):
            start_position = position
            break
    result: list[tuple[int, NumberingMarker]] = []
    last_value = -1
    for entry in entries[start_position:]:
        value = entry[1].path[0]
        if value > last_value:
            result.append(entry)
            last_value = value
    return result


def _peer_indent_entries(
    entries: list[tuple[int, NumberingMarker]],
    blocks: list[DocumentBlock],
) -> list[tuple[int, NumberingMarker]]:
    """Use indentation as supporting evidence when the source exposes it."""
    known = [(entry, _block_indent(blocks[entry[0]])) for entry in entries]
    known_indents = [indent for _, indent in known if indent is not None]
    if len(known_indents) < 2:
        return entries
    base = min(known_indents)
    peers = [entry for entry, indent in known if indent is None or abs(indent - base) <= 120]
    return peers if len(peers) >= 2 else entries


def _block_indent(block: DocumentBlock) -> int | None:
    left_indent = block.metadata.get("left_indent_twips")
    if isinstance(left_indent, int):
        return left_indent
    leading = block.metadata.get("leading_whitespace")
    if isinstance(leading, int):
        return leading * 240
    return None


def _extract_chinese_units(
    blocks: list[DocumentBlock],
    markers: dict[int, NumberingMarker],
    start: int,
    end: int,
) -> tuple[list[RetrievalUnit], int, int]:
    entries = [
        (index, marker)
        for index, marker in markers.items()
        if start <= index < end and marker.kind in {MarkerKind.CHINESE_HEADING, MarkerKind.PAREN_CHINESE}
    ]
    top_entries = _peer_indent_entries(
        [(index, marker) for index, marker in entries if marker.kind == MarkerKind.CHINESE_HEADING],
        blocks,
    )
    units: list[RetrievalUnit] = []
    if not top_entries:
        boundaries = [(index, marker) for index, marker in entries if marker.kind == MarkerKind.PAREN_CHINESE]
        for position, (unit_start, marker) in enumerate(boundaries):
            unit_end = boundaries[position + 1][0] if position + 1 < len(boundaries) else end
            unit_blocks = _retrieval_blocks(blocks, unit_start, unit_end)
            if unit_blocks:
                units.append(_build_unit(unit_blocks, marker.raw, len(units) + 1, CHINESE_PROFILE))
        return units, 2, len(entries)

    for top_position, (top_index, top_marker) in enumerate(top_entries):
        section_end = top_entries[top_position + 1][0] if top_position + 1 < len(top_entries) else end
        children = [
            (index, marker)
            for index, marker in entries
            if top_index < index < section_end and marker.kind == MarkerKind.PAREN_CHINESE
        ]
        if children:
            for child_position, (unit_start, marker) in enumerate(children):
                unit_end = children[child_position + 1][0] if child_position + 1 < len(children) else section_end
                unit_blocks = _retrieval_blocks(blocks, unit_start, unit_end)
                if unit_blocks:
                    heading = _heading_context(blocks, start, top_index) + [blocks[top_index].text]
                    units.append(
                        _build_unit(unit_blocks, marker.raw, len(units) + 1, CHINESE_PROFILE, heading)
                    )
        else:
            unit_blocks = _retrieval_blocks(blocks, top_index, section_end)
            if unit_blocks:
                units.append(
                    _build_unit(
                        unit_blocks,
                        top_marker.raw,
                        len(units) + 1,
                        CHINESE_PROFILE,
                        _heading_context(blocks, start, top_index),
                    )
                )
    depth = 2 if any(marker.kind == MarkerKind.PAREN_CHINESE for _, marker in entries) else 1
    return units, depth, len(entries)


def _retrieval_blocks(blocks: list[DocumentBlock], start: int, end: int) -> list[DocumentBlock]:
    return [
        block
        for block in blocks[start:end]
        if block.block_type not in {"heading", "footer"} and not is_toc_entry(block.text)
    ]


def _heading_context(blocks: list[DocumentBlock], region_start: int, unit_start: int) -> list[str]:
    for index in range(unit_start - 1, max(-1, region_start - 1), -1):
        block = blocks[index]
        marker = detect_numbering_marker(block.text)
        if block.block_type == "heading" or (marker and marker.kind == MarkerKind.APPENDIX):
            return [block.text]
    return []


def _region_payload(
    blocks: list[DocumentBlock],
    start: int,
    end: int,
    profile: str,
    marker_count: int,
    retrieval_depth: int,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "block_start_ordinal": blocks[start].ordinal if start < len(blocks) else None,
        "block_end_ordinal": blocks[end - 1].ordinal if end > start else None,
        "marker_count": marker_count,
        "retrieval_depth": retrieval_depth,
    }


def _structure_warnings(
    units: list[RetrievalUnit],
    markers: dict[int, NumberingMarker],
    profiles: list[str],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if len(profiles) > 1:
        warnings.append(
            {
                "code": "mixed_numbering_profiles",
                "severity": "review",
                "message": "文档存在多个独立编号区域，已分别套用模板切分。",
                "profiles": profiles,
            }
        )
    labels = Counter((unit.unit_type, unit.clause_no_raw) for unit in units)
    duplicates = [label for label, count in labels.items() if count > 1]
    if duplicates:
        warnings.append(
            {
                "code": "duplicate_clause_number",
                "severity": "warning",
                "message": f"发现 {len(duplicates)} 组重复编号；仍按文档顺序保存为不同 unit。",
                "labels": [f"{profile}:{label}" for profile, label in duplicates[:50]],
            }
        )
    decimal_labels = [
        marker.normalized for marker in markers.values() if marker.kind == MarkerKind.DECIMAL
    ]
    repeated_decimal = [label for label, count in Counter(decimal_labels).items() if count > 1]
    if repeated_decimal:
        warnings.append(
            {
                "code": "duplicate_decimal_number",
                "severity": "warning",
                "message": "小数层级编号存在重复，未据此丢弃正文。",
                "labels": repeated_decimal[:50],
            }
        )
    return warnings


def _build_unit(
    blocks: list[DocumentBlock],
    clause_no_raw: str,
    ordinal: int,
    unit_type: str,
    heading_path: list[str] | None = None,
) -> RetrievalUnit:
    text = "\n".join(block.text for block in blocks).strip()
    pages = [block.page_no for block in blocks if block.page_no is not None]
    paragraphs = [block.paragraph_index for block in blocks if block.paragraph_index is not None]
    char_starts = [block.char_start for block in blocks if block.char_start is not None]
    char_ends = [block.char_end for block in blocks if block.char_end is not None]
    return RetrievalUnit(
        id=uuid.uuid4().hex,
        clause_no_raw=clause_no_raw,
        clause_ordinal=ordinal,
        text=text,
        heading_path=list(heading_path if heading_path is not None else blocks[0].heading_path),
        source_block_ids=[block.id for block in blocks],
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        paragraph_start=min(paragraphs) if paragraphs else None,
        paragraph_end=max(paragraphs) if paragraphs else None,
        char_start=min(char_starts) if char_starts else None,
        char_end=max(char_ends) if char_ends else None,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        unit_type=unit_type,
    )
