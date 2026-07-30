from __future__ import annotations

import hashlib

from proof.infrastructure.postgres.migrate import (
    MIGRATIONS_DIR,
    _record_legacy_migration_aliases,
)


class MigrationConnection:
    def __init__(self, versions: dict[str, str]) -> None:
        self.versions = dict(versions)

    def execute(self, sql: str, values: tuple[str, ...]):
        version = values[0]
        if sql.startswith("SELECT 1"):
            return Result((1,) if version in self.versions else None)
        if sql.startswith("SELECT checksum"):
            checksum = self.versions.get(version)
            return Result((checksum,) if checksum else None)
        if sql.startswith("INSERT INTO proof_schema_migration"):
            self.versions[version] = values[1]
            return Result(None)
        raise AssertionError(sql)


class Result:
    def __init__(self, row) -> None:
        self.row = row

    def fetchone(self):
        return self.row


def test_legacy_summary_migration_is_aliased_to_current_version() -> None:
    connection = MigrationConnection({"007_policy_summary_and_finding_category": "legacy-checksum"})

    _record_legacy_migration_aliases(connection)

    expected = hashlib.sha256(
        (MIGRATIONS_DIR / "008_policy_summary_and_finding_category.sql").read_bytes()
    ).hexdigest()
    assert connection.versions["008_policy_summary_and_finding_category"] == expected


def test_alias_is_not_recorded_without_legacy_migration() -> None:
    connection = MigrationConnection({})

    _record_legacy_migration_aliases(connection)

    assert connection.versions == {}


def test_intra_conflict_migration_has_minimal_temp_vectors_and_cascades() -> None:
    sql = (MIGRATIONS_DIR / "011_intra_conflict_audit.sql").read_text("utf-8")

    assert "intra_conflict_status" in sql
    assert "intra_conflict_error_message" in sql
    assert "CREATE TABLE proof_draft_retrieval_embedding" in sql
    table_sql = sql.split(
        "CREATE TABLE proof_draft_retrieval_embedding", maxsplit=1
    )[1].split(");", maxsplit=1)[0]
    for column in (
        "audit_run_id", "retrieval_unit_id", "profile_id",
        "dimensions", "embedding", "created_at",
    ):
        assert column in table_sql
    assert "PRIMARY KEY (audit_run_id, retrieval_unit_id)" in table_sql
    assert table_sql.count("ON DELETE CASCADE") == 2
    assert "CREATE TABLE proof_intra_conflict_audit_finding" in sql
    assert "expires_at" not in sql


def test_intra_conflict_warning_migration_is_structured_and_cascades():
    sql = (
        MIGRATIONS_DIR / "013_intra_conflict_audit_warnings.sql"
    ).read_text("utf-8")

    assert "CREATE TABLE proof_intra_conflict_audit_warning" in sql
    assert "target_unit_id" in sql
    assert "finding_index" in sql
    assert "details jsonb" in sql
    assert sql.count("ON DELETE CASCADE") == 2


def test_policy_level_name_migration_keeps_stable_codes() -> None:
    sql = (MIGRATIONS_DIR / "012_policy_level_names.sql").read_text("utf-8")

    assert "WHEN 'upper' THEN '一级制度'" in sql
    assert "WHEN 'peer' THEN '二级制度'" in sql
    assert "WHEN 'lower' THEN '三级制度'" in sql
    assert "INSERT" not in sql
    assert "DELETE" not in sql


def test_tenant_isolation_migration_uses_minimal_root_fields_and_scoped_views() -> None:
    sql = (MIGRATIONS_DIR / "014_tenant_isolation.sql").read_text("utf-8")

    for table in ("proof_policy", "proof_document", "proof_ingestion_run"):
        statement = sql.split(f"ALTER TABLE {table}", maxsplit=1)[1].split(";", maxsplit=1)[0]
        assert "ADD COLUMN tenant_id text" in statement
        assert f"UPDATE {table} SET tenant_id = '1' WHERE tenant_id IS NULL" in sql
        assert f"ALTER TABLE {table}" in sql
    assert "DEFAULT '1'" not in sql

    assert "proof_document_tenant_content_hash_key" in sql
    assert "UNIQUE (tenant_id, content_hash)" in sql
    assert "proof_tenant_owns_document" in sql
    assert "proof_tenant_audit_run_v" in sql
    assert sql.count("current_setting('proof.tenant_id', true)") >= 3
    for child_table in (
        "proof_document_block",
        "proof_retrieval_unit",
        "proof_audit_run",
        "proof_audit_finding",
    ):
        assert f"ALTER TABLE {child_table}\n  ADD COLUMN tenant_id" not in sql


def test_similarity_version_migration_uses_integer_versions_and_retires_embeddings() -> None:
    sql = (MIGRATIONS_DIR / "015_policy_similarity_versioning.sql").read_text("utf-8")

    for column in (
        "family_id",
        "version_seq",
        "supersedes_policy_id",
        "similarity_state",
        "similarity_report",
    ):
        assert column in sql
    assert "UNIQUE (tenant_id, family_id, version_seq)" in sql
    assert "WHERE status = 'effective'" in sql
    assert "'retired'" in sql


def test_lifecycle_migration_carries_decimal_versions_and_keeps_delete_tombstones() -> None:
    sql = (MIGRATIONS_DIR / "016_policy_lifecycle.sql").read_text("utf-8")

    assert "version_seq / 100" in sql
    assert "(version_seq % 100) / 10" in sql
    assert "version_seq % 10" in sql
    assert "CREATE TABLE proof_policy_delete_tombstone" in sql
    assert "original_storage_path" in sql
    assert "trash_storage_path" in sql


def test_effective_policy_title_migration_keeps_only_highest_version() -> None:
    sql = (MIGRATIONS_DIR / "017_effective_policy_title_uniqueness.sql").read_text("utf-8")

    assert "PARTITION BY tenant_id, normalized_title" in sql
    assert "ORDER BY version_seq DESC" in sql
    assert "SET status = 'expired'" in sql
    assert "proof_policy_one_effective_normalized_title" in sql
    assert "WHERE status = 'effective'" in sql
