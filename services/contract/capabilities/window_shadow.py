"""Deterministic Legacy/Window Contract IR shadow comparison.

The comparison is test-only.  It never selects a production result and does
not treat either engine as ground truth.  Agreement is measured by category
and source position so model wording differences do not hide source overlap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.contract.capabilities.register import ContractIrSemanticDelta
from services.contract.capabilities.window_pipeline import PipelineSemanticIr


IR_FIELDS = (
    "definitions",
    "rights",
    "obligations",
    "prohibitions",
    "payment_terms",
    "delivery_terms",
    "acceptance_terms",
    "liabilities",
    "termination_terms",
    "confidentiality_terms",
    "intellectual_property_terms",
    "dispute_resolution",
    "dates",
    "amounts",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShadowSourceBlock(StrictModel):
    block_id: str
    block_no: int = Field(gt=0)
    text: str = Field(min_length=1)


class ShadowCompareRequest(StrictModel):
    legacy_ir: ContractIrSemanticDelta
    window_ir: PipelineSemanticIr
    legacy_blocks: list[ShadowSourceBlock] = Field(default_factory=list)
    window_blocks: list[ShadowSourceBlock] = Field(default_factory=list)


class ShadowAnchor(StrictModel):
    block_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class ShadowItem(StrictModel):
    field: str
    item_id: str | None = None
    label: str
    anchors: list[ShadowAnchor] = Field(min_length=1)


class ShadowMatch(StrictModel):
    field: str
    match_type: Literal["EXACT_ANCHOR", "OVERLAPPING_ANCHOR"]
    overlap_score: float = Field(ge=0, le=1)
    legacy: ShadowItem
    window: ShadowItem


class ShadowFieldComparison(StrictModel):
    field: str
    legacy_count: int = Field(ge=0)
    window_count: int = Field(ge=0)
    exact_anchor_match_count: int = Field(ge=0)
    overlapping_anchor_match_count: int = Field(ge=0)
    legacy_only_count: int = Field(ge=0)
    window_only_count: int = Field(ge=0)
    legacy_anchor_agreement: float | None = Field(default=None, ge=0, le=1)
    window_anchor_agreement: float | None = Field(default=None, ge=0, le=1)


class ShadowCompareResult(StrictModel):
    comparison_basis: Literal["CATEGORY_AND_SOURCE_ANCHOR"] = "CATEGORY_AND_SOURCE_ANCHOR"
    ground_truth_available: Literal[False] = False
    legacy_count: int = Field(ge=0)
    window_count: int = Field(ge=0)
    exact_anchor_match_count: int = Field(ge=0)
    overlapping_anchor_match_count: int = Field(ge=0)
    legacy_only_count: int = Field(ge=0)
    window_only_count: int = Field(ge=0)
    legacy_anchor_agreement: float | None = Field(default=None, ge=0, le=1)
    window_anchor_agreement: float | None = Field(default=None, ge=0, le=1)
    fields: list[ShadowFieldComparison]
    matches: list[ShadowMatch]
    legacy_only: list[ShadowItem]
    window_only: list[ShadowItem]
    interpretation: str = (
        "该结果只衡量同类别原文位置的一致性，不代表法律准确率；"
        "Legacy 独有和 Window 独有项都需要人工复核。"
    )


@dataclass(frozen=True, slots=True)
class _ComparableItem:
    view: ShadowItem
    anchor_key: tuple[tuple[str, int, int], ...]


def compare_contract_ir(request: ShadowCompareRequest) -> ShadowCompareResult:
    field_results: list[ShadowFieldComparison] = []
    matches: list[ShadowMatch] = []
    legacy_only: list[ShadowItem] = []
    window_only: list[ShadowItem] = []
    legacy_block_ids, window_block_ids = _cross_generation_block_ids(
        request.legacy_blocks,
        request.window_blocks,
    )

    for field in IR_FIELDS:
        legacy_items = [
            _to_item(field, item, legacy_block_ids)
            for item in getattr(request.legacy_ir, field)
        ]
        window_items = [
            _to_item(field, item, window_block_ids)
            for item in getattr(request.window_ir, field)
        ]
        field_matches, remaining_legacy, remaining_window = _match_items(
            legacy_items,
            window_items,
        )
        matches.extend(field_matches)
        legacy_only.extend(item.view for item in remaining_legacy)
        window_only.extend(item.view for item in remaining_window)
        exact_count = sum(item.match_type == "EXACT_ANCHOR" for item in field_matches)
        overlap_count = len(field_matches) - exact_count
        field_results.append(
            ShadowFieldComparison(
                field=field,
                legacy_count=len(legacy_items),
                window_count=len(window_items),
                exact_anchor_match_count=exact_count,
                overlapping_anchor_match_count=overlap_count,
                legacy_only_count=len(remaining_legacy),
                window_only_count=len(remaining_window),
                legacy_anchor_agreement=_ratio(len(field_matches), len(legacy_items)),
                window_anchor_agreement=_ratio(len(field_matches), len(window_items)),
            )
        )

    legacy_count = sum(item.legacy_count for item in field_results)
    window_count = sum(item.window_count for item in field_results)
    exact_count = sum(item.exact_anchor_match_count for item in field_results)
    overlap_count = sum(item.overlapping_anchor_match_count for item in field_results)
    matched_count = exact_count + overlap_count
    return ShadowCompareResult(
        legacy_count=legacy_count,
        window_count=window_count,
        exact_anchor_match_count=exact_count,
        overlapping_anchor_match_count=overlap_count,
        legacy_only_count=len(legacy_only),
        window_only_count=len(window_only),
        legacy_anchor_agreement=_ratio(matched_count, legacy_count),
        window_anchor_agreement=_ratio(matched_count, window_count),
        fields=field_results,
        matches=matches,
        legacy_only=legacy_only,
        window_only=window_only,
    )


def _to_item(
    field: str,
    value,
    block_ids: dict[str, str],
) -> _ComparableItem:
    anchors = [
        ShadowAnchor(
            block_id=anchor.block_id,
            char_start=anchor.char_start,
            char_end=anchor.char_end,
        )
        for anchor in value.source_anchors
    ]
    if field == "definitions":
        label = f"{value.term}：{value.meaning}"
        item_id = None
    else:
        parts = [value.subject, value.predicate, value.object]
        label = " | ".join(str(part) for part in parts if part)
        item_id = value.item_id
    anchor_key = tuple(
        sorted(
            (
                block_ids.get(anchor.block_id, anchor.block_id),
                anchor.char_start,
                anchor.char_end,
            )
            for anchor in anchors
        )
    )
    return _ComparableItem(
        view=ShadowItem(field=field, item_id=item_id, label=label, anchors=anchors),
        anchor_key=anchor_key,
    )


def _match_items(
    legacy_items: list[_ComparableItem],
    window_items: list[_ComparableItem],
) -> tuple[list[ShadowMatch], list[_ComparableItem], list[_ComparableItem]]:
    matches: list[ShadowMatch] = []
    unmatched_legacy = set(range(len(legacy_items)))
    unmatched_window = set(range(len(window_items)))

    for legacy_index in range(len(legacy_items)):
        exact_window = next(
            (
                window_index
                for window_index in sorted(unmatched_window)
                if legacy_items[legacy_index].anchor_key == window_items[window_index].anchor_key
            ),
            None,
        )
        if exact_window is None:
            continue
        matches.append(
            _match(legacy_items[legacy_index], window_items[exact_window], "EXACT_ANCHOR", 1.0)
        )
        unmatched_legacy.remove(legacy_index)
        unmatched_window.remove(exact_window)

    candidates: list[tuple[float, int, int]] = []
    for legacy_index in unmatched_legacy:
        for window_index in unmatched_window:
            score = _overlap_score(legacy_items[legacy_index], window_items[window_index])
            if score > 0:
                candidates.append((score, legacy_index, window_index))
    for score, legacy_index, window_index in sorted(
        candidates,
        key=lambda item: (-item[0], item[1], item[2]),
    ):
        if legacy_index not in unmatched_legacy or window_index not in unmatched_window:
            continue
        matches.append(
            _match(
                legacy_items[legacy_index],
                window_items[window_index],
                "OVERLAPPING_ANCHOR",
                score,
            )
        )
        unmatched_legacy.remove(legacy_index)
        unmatched_window.remove(window_index)

    return (
        matches,
        [legacy_items[index] for index in sorted(unmatched_legacy)],
        [window_items[index] for index in sorted(unmatched_window)],
    )


def _match(
    legacy: _ComparableItem,
    window: _ComparableItem,
    match_type: Literal["EXACT_ANCHOR", "OVERLAPPING_ANCHOR"],
    score: float,
) -> ShadowMatch:
    return ShadowMatch(
        field=legacy.view.field,
        match_type=match_type,
        overlap_score=round(score, 6),
        legacy=legacy.view,
        window=window.view,
    )


def _overlap_score(legacy: _ComparableItem, window: _ComparableItem) -> float:
    intersection = 0
    for legacy_block, legacy_start, legacy_end in legacy.anchor_key:
        for window_block, window_start, window_end in window.anchor_key:
            if legacy_block != window_block:
                continue
            intersection += max(
                0,
                min(legacy_end, window_end) - max(legacy_start, window_start),
            )
    if intersection == 0:
        return 0.0
    legacy_length = sum(item.char_end - item.char_start for item in legacy.view.anchors)
    window_length = sum(item.char_end - item.char_start for item in window.view.anchors)
    union = legacy_length + window_length - intersection
    return min(1.0, intersection / union) if union > 0 else 0.0


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _cross_generation_block_ids(
    legacy_blocks: list[ShadowSourceBlock],
    window_blocks: list[ShadowSourceBlock],
) -> tuple[dict[str, str], dict[str, str]]:
    legacy = _block_signatures(legacy_blocks)
    window = _block_signatures(window_blocks)
    shared = set(legacy.values()) & set(window.values())
    return (
        {block_id: signature for block_id, signature in legacy.items() if signature in shared},
        {block_id: signature for block_id, signature in window.items() if signature in shared},
    )


def _block_signatures(blocks: list[ShadowSourceBlock]) -> dict[str, str]:
    return {
        block.block_id: (
            f"block:{block.block_no}:"
            f"{hashlib.sha256(block.text.encode('utf-8')).hexdigest()}"
        )
        for block in blocks
    }


__all__ = [
    "ShadowCompareRequest",
    "ShadowCompareResult",
    "ShadowSourceBlock",
    "compare_contract_ir",
]
