from __future__ import annotations

import re


_POLICY_VERSION_RE = re.compile(r"^v([1-9]\d*)\.([0-9])\.([0-9])$", re.IGNORECASE)


def format_policy_version(version_seq: int) -> str:
    """Render a monotonic sequence as v<major>.<minor>.<patch> with decimal carry."""

    sequence = int(version_seq)
    if sequence < 0:
        raise ValueError("version_seq must not be negative")
    major = 1 + sequence // 100
    minor = (sequence % 100) // 10
    patch = sequence % 10
    return f"v{major}.{minor}.{patch}"


def parse_policy_version(value: str) -> tuple[str, int]:
    """Validate and convert v<major>.<minor>.<patch> into the monotonic sequence."""

    normalized = str(value or "").strip().lower()
    match = _POLICY_VERSION_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "Policy version must use v<major>.<minor>.<patch>, for example v1.0.0."
        )
    major, minor, patch = (int(part) for part in match.groups())
    sequence = (major - 1) * 100 + minor * 10 + patch
    return format_policy_version(sequence), sequence
