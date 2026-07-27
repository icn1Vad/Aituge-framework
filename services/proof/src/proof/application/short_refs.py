from __future__ import annotations

from typing import Any, Iterable


def short_ref(prefix: str, index: int) -> str:
    if len(prefix) != 1 or not prefix.isalpha() or index < 1 or index > 99:
        raise ValueError("Short refs require one letter and an index from 1 to 99.")
    return f"{prefix.upper()}{index:02d}"


def attach_short_refs(
    items: Iterable[dict[str, Any]],
    *,
    prefix: str,
) -> list[dict[str, Any]]:
    return [
        {"ref": short_ref(prefix, index), **item}
        for index, item in enumerate(items, start=1)
    ]


def ref_id_map(items: Iterable[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Short-ref items must be objects.")
        ref = str(item.get("ref") or "").strip().upper()
        item_id = str(item.get("id") or "").strip()
        if not ref or not item_id or ref in mapping:
            raise ValueError("Short-ref items must have unique refs and non-blank IDs.")
        mapping[ref] = item_id
    return mapping
