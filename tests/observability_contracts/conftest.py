from __future__ import annotations

import pytest

from .contract_loader import load_external, load_internal


@pytest.fixture(scope="session")
def external_spec() -> dict:
    return load_external()


@pytest.fixture(scope="session")
def internal_spec() -> dict:
    return load_internal()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "implementation: requires an explicitly supplied implementation worktree",
    )
