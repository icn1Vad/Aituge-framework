from __future__ import annotations

from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository


def test_party_source_span_prefers_exact_contract_text() -> None:
    source = "乙方：云杉数智(上海)有限公司"

    span = FrameworkCallbackRepository._party_source_span(
        "云杉数智(上海)有限公司",
        source,
    )

    assert span is not None
    assert source[slice(*span)] == "云杉数智(上海)有限公司"


def test_party_source_span_maps_equivalent_parentheses_to_original_offsets() -> None:
    source = "乙方：云杉数智（上海）有限公司（盖章）"

    span = FrameworkCallbackRepository._party_source_span(
        "云杉数智(上海)有限公司",
        source,
    )

    assert span is not None
    assert source[slice(*span)] == "云杉数智（上海）有限公司"


def test_party_source_span_rejects_a_different_legal_entity() -> None:
    source = "乙方：云杉数智（上海）有限公司"

    span = FrameworkCallbackRepository._party_source_span(
        "云杉数智（北京）有限公司",
        source,
    )

    assert span is None


def test_materialized_party_anchors_the_original_contract_characters() -> None:
    source = "乙方：云杉数智（上海）有限公司（盖章）"
    party = FrameworkCallbackRepository._materialize_party(
        "PARTY_B",
        "云杉数智(上海)有限公司",
        [{"block_id": "block-1", "page_number": 1, "text": source}],
        "generation-1",
    )

    anchor = party["source_anchors"][0]
    assert source[anchor["char_start"] : anchor["char_end"]] == "云杉数智（上海）有限公司"
