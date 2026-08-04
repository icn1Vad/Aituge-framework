from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_TABLE = "tuge_model_observability_schema_migration"
MIGRATION_LOCK_ID = 1_827_002
_LEGACY_UP_ONLY_CHECKSUMS = {
    "001_model_invocation_ledger": (
        "a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be"
    ),
}
_APPROVED_PAIR_CHECKSUMS = {
    "001_model_invocation_ledger": (
        "480f02d221f9f4f4b35bfbce60e1b0ec337e135ae81944cf7955b7b2189a3da8"
    ),
}
_APPROVED_SOURCE_FILENAMES = {
    "001_model_invocation_ledger": (
        "001_model_invocation_ledger.up.sql",
        "001_model_invocation_ledger.down.sql",
    ),
}
_CHECKSUM_FORMAT = b"model-observability-migration-pair-v1\0"


class ModelObservabilityMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    up_path: Path
    down_path: Path

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256()
        digest.update(_CHECKSUM_FORMAT)
        for path in (self.up_path, self.down_path):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @property
    def up_checksum(self) -> str:
        return hashlib.sha256(self.up_path.read_bytes()).hexdigest()

    def assert_source_integrity(self) -> None:
        approved_pair = _APPROVED_PAIR_CHECKSUMS.get(self.version)
        if approved_pair is None:
            return
        approved_names = _APPROVED_SOURCE_FILENAMES[self.version]
        approved_up = _LEGACY_UP_ONLY_CHECKSUMS[self.version]
        if (
            (self.up_path.name, self.down_path.name) != approved_names
            or self.up_checksum != approved_up
            or self.checksum != approved_pair
        ):
            raise ModelObservabilityMigrationError(
                f"Migration {self.version} source integrity check failed"
            )

    def accepts_stored_checksum(self, value: str) -> bool:
        try:
            self.assert_source_integrity()
        except ModelObservabilityMigrationError:
            return False
        return value == self.checksum or value == _LEGACY_UP_ONLY_CHECKSUMS.get(
            self.version
        )


def discover_migrations() -> list[Migration]:
    migrations: list[Migration] = []
    for up_path in sorted(MIGRATIONS_DIR.glob("*.up.sql")):
        version = up_path.name.removesuffix(".up.sql")
        down_path = MIGRATIONS_DIR / f"{version}.down.sql"
        if not down_path.is_file():
            raise ModelObservabilityMigrationError(
                f"Missing rollback SQL for model migration {version}"
            )
        migrations.append(Migration(version, up_path, down_path))
    if not migrations:
        raise ModelObservabilityMigrationError("No model migrations found")
    discovered_versions = {migration.version for migration in migrations}
    missing_approved = set(_APPROVED_PAIR_CHECKSUMS) - discovered_versions
    if missing_approved:
        raise ModelObservabilityMigrationError(
            "Missing approved model migration: " + ", ".join(sorted(missing_approved))
        )
    return migrations


def run_migrations(database_url: str) -> list[str]:
    """Apply immutable page-2 migrations to an explicitly supplied database."""

    normalized_url = _require_database_url(database_url)
    applied: list[str] = []
    with psycopg.connect(normalized_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        _ensure_migration_table(connection)
        for migration in discover_migrations():
            migration.assert_source_integrity()
            row = connection.execute(
                f"SELECT checksum FROM {MIGRATION_TABLE} WHERE version = %s",
                (migration.version,),
            ).fetchone()
            if row is not None:
                if not migration.accepts_stored_checksum(row[0]):
                    raise ModelObservabilityMigrationError(
                        f"Migration {migration.version} changed after application"
                    )
                if row[0] != migration.checksum:
                    connection.execute(
                        f"UPDATE {MIGRATION_TABLE} SET checksum = %s "
                        "WHERE version = %s",
                        (migration.checksum, migration.version),
                    )
                continue
            connection.execute(migration.up_path.read_text("utf-8"))
            connection.execute(
                f"INSERT INTO {MIGRATION_TABLE} (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
            applied.append(migration.version)
        connection.commit()
    return applied


def rollback_migration(
    database_url: str,
    *,
    version: str,
    confirm_version: str,
) -> str:
    """Rollback only the latest revision after an exact explicit confirmation."""

    if confirm_version != version:
        raise ModelObservabilityMigrationError(
            "Rollback confirmation must exactly match the migration version"
        )
    normalized_url = _require_database_url(database_url)
    migrations = {item.version: item for item in discover_migrations()}
    migration = migrations.get(version)
    if migration is None:
        raise ModelObservabilityMigrationError(f"Unknown migration: {version}")
    migration.assert_source_integrity()
    with psycopg.connect(normalized_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        _ensure_migration_table(connection)
        rows = connection.execute(
            f"SELECT version, checksum FROM {MIGRATION_TABLE} "
            "ORDER BY applied_at DESC, version DESC"
        ).fetchall()
        if not rows or rows[0][0] != version:
            raise ModelObservabilityMigrationError(
                "Only the latest applied model migration can be rolled back"
            )
        if not migration.accepts_stored_checksum(rows[0][1]):
            raise ModelObservabilityMigrationError(
                f"Migration {version} changed after application"
            )
        try:
            connection.execute(migration.down_path.read_text("utf-8"))
        except psycopg.errors.CheckViolation as exc:
            if (
                getattr(exc.diag, "message_primary", "")
                == "MODEL_OBSERVABILITY_ROLLBACK_CLOCK_SKEW_DATA"
            ):
                raise ModelObservabilityMigrationError(
                    "MODEL_OBSERVABILITY_ROLLBACK_CLOCK_SKEW_DATA"
                ) from None
            raise
        connection.execute(
            f"DELETE FROM {MIGRATION_TABLE} WHERE version = %s",
            (version,),
        )
        connection.commit()
    return version


def _ensure_migration_table(connection: psycopg.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
          version text PRIMARY KEY,
          checksum text NOT NULL,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def _require_database_url(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ModelObservabilityMigrationError(
            "An explicit MODEL_OBSERVABILITY_DATABASE_URL is required"
        )
    if normalized.startswith("postgresql+asyncpg://"):
        normalized = "postgresql://" + normalized.removeprefix(
            "postgresql+asyncpg://"
        )
    if not normalized.startswith(("postgresql://", "postgres://")):
        raise ModelObservabilityMigrationError(
            "Model observability migrations only support PostgreSQL"
        )
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Model observability schema migration")
    parser.add_argument("--rollback")
    parser.add_argument("--confirm-version")
    args = parser.parse_args()
    database_url = os.getenv("MODEL_OBSERVABILITY_DATABASE_URL", "")
    if args.rollback:
        rolled_back = rollback_migration(
            database_url,
            version=args.rollback,
            confirm_version=args.confirm_version or "",
        )
        print(f"Rolled back model observability migration: {rolled_back}")
        return
    applied = run_migrations(database_url)
    if applied:
        print("Applied model observability migrations: " + ", ".join(applied))
    else:
        print("Model observability database is up to date")


if __name__ == "__main__":
    main()
