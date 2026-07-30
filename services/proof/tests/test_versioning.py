from __future__ import annotations

import pytest

from proof.versioning import format_policy_version, parse_policy_version


@pytest.mark.parametrize(
    ("raw", "normalized", "sequence"),
    [
        ("v1.0.0", "v1.0.0", 0),
        ("V1.0.9", "v1.0.9", 9),
        (" v2.3.4 ", "v2.3.4", 134),
    ],
)
def test_parse_policy_version_matches_existing_sequence(
    raw: str,
    normalized: str,
    sequence: int,
) -> None:
    assert parse_policy_version(raw) == (normalized, sequence)
    assert format_policy_version(sequence) == normalized


@pytest.mark.parametrize("value", ["1.0.0", "v0.0.0", "v1.10.0", "v1.0", "draft"])
def test_parse_policy_version_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_policy_version(value)
