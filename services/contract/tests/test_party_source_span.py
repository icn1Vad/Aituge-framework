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


def test_materialized_role_placeholder_can_exist_without_a_false_source_anchor() -> None:
    party = FrameworkCallbackRepository._materialize_party(
        "PARTY_A",
        "甲方",
        [{"block_id": "block-1", "page_number": 1, "text": "本合同约定如下"}],
        "generation-1",
        name_resolved=False,
    )

    assert party == {
        "role": "PARTY_A",
        "name": "甲方",
        "name_resolved": False,
        "source_anchors": [],
    }


def test_party_unresolved_handles_partial_candidates_without_type_error() -> None:
    error = FrameworkCallbackRepository._party_unresolved(
        {"perspective": "PARTY_A"},
        {
            "resolution_status": "PARTIAL",
            "party_a": None,
            "party_b": {"name": "乙方公司"},
        },
        "Contract party names could not be fully resolved",
    )

    assert error.code == "PARTY_UNRESOLVED"
    assert error.status_code == 422
    assert error.details == {
        "perspective": "PARTY_A",
        "candidate_parties": ["乙方公司"],
    }
