from __future__ import annotations

import hashlib

from proof.infrastructure.postgres.migrate import MIGRATIONS_DIR, _record_legacy_migration_aliases


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
