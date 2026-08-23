from types import SimpleNamespace

import pytest

from services.contract.capabilities.register import (
    _confirmed_party_value,
    _resolved_party_value,
)
from task_manager.pipeline.errors import StageExecutionError


def candidate(role: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, name=name)


def test_explicit_empty_label_is_not_stated() -> None:
    party = _resolved_party_value([], frozenset({"PARTY_A"}), "PARTY_A")
    assert party.name == "甲方"
    assert party.name_resolved is False
    assert party.name_status == "NOT_STATED"


def test_no_label_is_unresolved_instead_of_not_stated() -> None:
    with pytest.raises(StageExecutionError) as exc_info:
        _resolved_party_value([], frozenset(), "PARTY_A")
    assert exc_info.value.code == "PARTY_UNRESOLVED"


def test_extracted_and_user_confirmed_statuses_are_distinct() -> None:
    extracted = _resolved_party_value(
        [candidate("PARTY_B", "乙公司")],
        frozenset({"PARTY_B"}),
        "PARTY_B",
    )
    confirmed = _confirmed_party_value("手工确认乙公司", "PARTY_B")
    assert extracted.name_status == "EXTRACTED"
    assert confirmed.name_status == "USER_CONFIRMED"


def test_confirmed_placeholder_preserves_not_stated_semantics() -> None:
    confirmed = _confirmed_party_value("乙方", "PARTY_B")
    assert confirmed.name_resolved is False
    assert confirmed.name_status == "NOT_STATED"


def test_multiple_distinct_candidates_remain_unresolved() -> None:
    with pytest.raises(StageExecutionError) as exc_info:
        _resolved_party_value(
            [candidate("PARTY_A", "甲公司一"), candidate("PARTY_A", "甲公司二")],
            frozenset({"PARTY_A"}),
            "PARTY_A",
        )
    assert exc_info.value.code == "PARTY_UNRESOLVED"
