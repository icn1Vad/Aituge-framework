from __future__ import annotations

import pytest

from proof.tenant import tenant_scope


@pytest.fixture(autouse=True)
def java_tenant_context():
    """Direct service tests execute with the tenant Java would inject."""

    with tenant_scope("1"):
        yield
