from __future__ import annotations

import pytest

from contract.config import Settings
from contract.errors import ConfigurationError
from contract.persistence.postgres.migrate import MIGRATIONS_DIR, run_migrations


EXPECTED_TABLES = {
    "contract_document",
    "contract_parse_generation",
    "contract_document_block",
    "contract_review_run",
    "contract_framework_attempt",
    "contract_review_stage_result",
    "contract_review_result",
}


def test_migration_requires_database_url() -> None:
    with pytest.raises(ConfigurationError):
        run_migrations(Settings(database_url=""))


def test_initial_migration_defines_all_frozen_technical_tables() -> None:
    sql = (MIGRATIONS_DIR / "001_initial.sql").read_text("utf-8")

    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table}" in sql
    assert "uq_contract_generation_succeeded" in sql
    assert "trg_contract_document_active_generation" in sql
    assert "uq_contract_attempt_active" in sql
    assert "UNIQUE (tenant_id, business_task_id)" in sql
    assert "UNIQUE (tenant_id, user_id, idempotency_key)" in sql
    assert "REFERENCES contract_document(id, tenant_id)" in sql
    assert "REFERENCES contract_parse_generation(id, tenant_id)" in sql
    assert "tenant_id, framework_task_id, framework_run_id" in sql
