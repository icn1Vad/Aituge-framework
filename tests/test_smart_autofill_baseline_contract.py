from __future__ import annotations

import json
from pathlib import Path


CONTRACT_DIR = Path(__file__).resolve().parents[1] / "docs" / "smart_autofill"


def load_json(name: str) -> dict:
    return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))


def test_field_schema_freezes_all_79_fields() -> None:
    schema = load_json("smart_fill_field_schema.json")
    fields = schema["fields"]
    field_ids = [field["field_id"] for field in fields]

    assert schema["group_count"] == 12
    assert schema["field_count"] == 79
    assert len(fields) == 79
    assert len(field_ids) == len(set(field_ids))
    assert sum(field["extraction_mode"] == "manual_only" for field in fields) == 2


def test_five_item_mapping_has_full_non_overlapping_coverage() -> None:
    schema = load_json("smart_fill_field_schema.json")
    mapping = load_json("smart_fill_group_mapping.json")
    expected = {field["field_id"] for field in schema["fields"]}
    items = mapping["items"]
    assigned = [field_id for item in items for field_id in item["allowed_field_ids"]]
    packages = [item["skill_package"] for item in items]

    assert len(items) == 5
    assert mapping["max_concurrency"] == 5
    assert len(packages) == len(set(packages)) == 5
    assert len(assigned) == len(set(assigned)) == 79
    assert set(assigned) == expected
    assert mapping["coverage"] == {
        "assigned_field_count": 79,
        "missing_field_ids": [],
        "duplicate_field_ids": [],
    }


def test_output_contract_supports_scalar_dynamic_and_evidence_values() -> None:
    schema = load_json("smart_fill_field_schema.json")
    contract = load_json("smart_fill_output_contract.json")
    field_ids = {field["field_id"] for field in schema["fields"]}
    dynamic_ids = set(contract["dynamic_value_contracts"])

    assert contract["confidence"]["enabled"] is False
    assert dynamic_ids <= field_ids
    assert contract["field_result"]["required"] == [
        "field_id",
        "status",
        "value",
        "evidence",
    ]
    evidence = contract["field_result"]["properties"]["evidence"]
    assert evidence["items"]["required"] == ["file_id", "quote"]
    assert contract["open_decisions"][0]["id"] == "t0_definition"
    assert contract["open_decisions"][0]["status"] == "pending_business_confirmation"


def test_benchmark_manifest_locks_pdf_and_docx_samples() -> None:
    manifest = load_json("benchmark_manifest.json")
    extensions = {item["extension"] for item in manifest["files"]}

    assert manifest["single_user_test_mode"] is True
    assert extensions == {".pdf", ".docx"}
    assert len(manifest["files"]) == 2
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    assert manifest["quality_baseline"]["smart_autofill_strict_accuracy"] == 0.703
    assert manifest["quality_baseline"]["smart_autofill_completeness"] == 0.81
