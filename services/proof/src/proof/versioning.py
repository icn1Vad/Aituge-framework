from __future__ import annotations


def format_policy_version(version_seq: int) -> str:
    """Render a monotonic sequence as v<major>.<minor>.<patch> with decimal carry."""

    sequence = int(version_seq)
    if sequence < 0:
        raise ValueError("version_seq must not be negative")
    major = 1 + sequence // 100
    minor = (sequence % 100) // 10
    patch = sequence % 10
    return f"v{major}.{minor}.{patch}"
