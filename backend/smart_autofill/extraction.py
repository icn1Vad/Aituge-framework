from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .normalization import EXTERNAL_DATA_FIELD_IDS


CONTRACT_DIR = Path(__file__).parents[2] / "docs" / "smart_autofill"


def build_extraction_input(document_ids: list[str]) -> dict[str, Any]:
    if not document_ids:
        raise ValueError("At least one document_id is required.")
    field_schema = _read_json("smart_fill_field_schema.json")
    group_mapping = _read_json("smart_fill_group_mapping.json")
    fields_by_id = {
        field["field_id"]: field
        for group in field_schema["groups"]
        for field in group["fields"]
    }
    items = []
    for mapping in group_mapping["items"]:
        manual_ids = set(mapping["manual_only_field_ids"])
        field_specs = []
        for field_id in mapping["allowed_field_ids"]:
            spec = dict(fields_by_id[field_id])
            if field_id in manual_ids:
                spec["extraction_mode"] = "manual_only"
            elif field_id in EXTERNAL_DATA_FIELD_IDS:
                spec["extraction_mode"] = "external_data_disabled"
            else:
                spec["extraction_mode"] = "automatic"
            field_specs.append(spec)
        items.append({
            "id": mapping["item_id"],
            "group_id": mapping["item_id"],
            "skill_package": mapping["skill_package"],
            "field_ids": mapping["allowed_field_ids"],
            "field_specs": field_specs,
            "extraction_instructions": (
                "Retrieve evidence from the listed document_ids. Return every field_id exactly once; "
                "use status=missing and value=null when unsupported. Manual-only fields must remain missing."
                " Fields marked external_data_disabled must also remain missing with value=null."
                " For financial forecasts, t0 is the investment occurrence year; keep t0 empty when the "
                "first disclosed forecast year is later, and align later years to t0+N."
            ),
        })
    return {
        "document_ids": document_ids,
        "items": items,
        "max_concurrency": 5,
        "failure_policy": "continue",
        "retry_per_item": 1,
        "source_context": {
            "knowledgebase_id": "smartfilldocs",
            "retrieval_tools": [
                "catalog-smartfil",
                "search-knowledgebase-smartfilld",
                "grep-smartfil",
                "fetch-smartfil",
            ],
            "field_schema_id": field_schema["schema_id"],
        },
    }


def _read_json(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
