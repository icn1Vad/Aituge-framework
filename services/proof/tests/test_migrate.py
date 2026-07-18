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
