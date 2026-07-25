from fastapi.testclient import TestClient

from services.contract.capabilities.window_extractor_api import create_app
from services.contract.capabilities.window_shadow import (
    ShadowCompareRequest,
    compare_contract_ir,
)


def _anchor(block_id: str, start: int, end: int) -> dict:
    return {
        "anchor_id": f"anchor-{block_id}-{start}-{end}",
        "block_id": block_id,
        "page_number": None,
        "char_start": start,
        "char_end": end,
    }


def _item(item_id: str, block_id: str, start: int, end: int) -> dict:
    return {
        "item_id": item_id,
        "subject": "甲方",
        "predicate": "应履行",
        "object": item_id,
        "source_anchors": [_anchor(block_id, start, end)],
    }


def _empty_ir() -> dict[str, list]:
    return {
        "definitions": [],
        "rights": [],
        "obligations": [],
        "prohibitions": [],
        "payment_terms": [],
        "delivery_terms": [],
        "acceptance_terms": [],
        "liabilities": [],
        "termination_terms": [],
        "confidentiality_terms": [],
        "intellectual_property_terms": [],
        "dispute_resolution": [],
        "dates": [],
        "amounts": [],
    }


def _request() -> ShadowCompareRequest:
    legacy = _empty_ir()
    legacy["obligations"] = [
        _item("legacy-exact", "block-1", 0, 10),
        _item("legacy-overlap", "block-2", 0, 20),
        _item("legacy-only", "block-3", 0, 10),
    ]
    window = _empty_ir()
    window["obligations"] = [
        _item("window-exact", "block-1", 0, 10),
        _item("window-overlap", "block-2", 5, 15),
        _item("window-only", "block-4", 0, 10),
    ]
    window["payment_terms"] = [_item("window-payment", "block-3", 0, 10)]
    return ShadowCompareRequest(legacy_ir=legacy, window_ir=window)


def test_shadow_compare_uses_category_and_source_anchor() -> None:
    result = compare_contract_ir(_request())

    assert result.ground_truth_available is False
    assert result.legacy_count == 3
    assert result.window_count == 4
    assert result.exact_anchor_match_count == 1
    assert result.overlapping_anchor_match_count == 1
    assert result.legacy_only_count == 1
    assert result.window_only_count == 2
    assert result.legacy_anchor_agreement == 0.666667
    assert result.window_anchor_agreement == 0.5
    assert result.matches[1].overlap_score == 0.5
    assert {item.item_id for item in result.legacy_only} == {"legacy-only"}
    assert {item.item_id for item in result.window_only} == {
        "window-only",
        "window-payment",
    }


def test_shadow_compare_empty_categories_do_not_claim_perfect_agreement() -> None:
    result = compare_contract_ir(
        ShadowCompareRequest(legacy_ir=_empty_ir(), window_ir=_empty_ir())
    )

    assert result.legacy_anchor_agreement is None
    assert result.window_anchor_agreement is None
    assert all(item.legacy_anchor_agreement is None for item in result.fields)
    assert all(item.window_anchor_agreement is None for item in result.fields)


def test_shadow_compare_maps_blocks_across_parse_generations_deterministically() -> None:
    legacy = _empty_ir()
    legacy["obligations"] = [_item("legacy", "legacy-block", 2, 8)]
    window = _empty_ir()
    window["obligations"] = [_item("window", "window-block", 2, 8)]

    result = compare_contract_ir(
        ShadowCompareRequest(
            legacy_ir=legacy,
            window_ir=window,
            legacy_blocks=[
                {"block_id": "legacy-block", "block_no": 7, "text": "相同合同原文"}
            ],
            window_blocks=[
                {"block_id": "window-block", "block_no": 7, "text": "相同合同原文"}
            ],
        )
    )

    assert result.exact_anchor_match_count == 1
    assert result.legacy_only_count == 0
    assert result.window_only_count == 0


def test_shadow_compare_test_api_returns_typed_result() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/api/shadow-compare", json=_request().model_dump(mode="json"))

    assert response.status_code == 200
    assert response.json()["comparison_basis"] == "CATEGORY_AND_SOURCE_ANCHOR"
    assert response.json()["exact_anchor_match_count"] == 1
