from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

from proof.config import Settings, get_settings
from proof.errors import ConfigurationError


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
LEGACY_MIGRATION_ALIASES = {
    "007_policy_summary_and_finding_category": "008_policy_summary_and_finding_category",
}


def run_migrations(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    if not settings.database_url:
        raise ConfigurationError("PROOF_DATABASE_URL is required to run migrations.")

    applied: list[str] = []
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proof_schema_migration (
              version text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        _record_legacy_migration_aliases(conn)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            sql = path.read_text("utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM proof_schema_migration WHERE version = %s",
                (version,),
            ).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f"Migration {version} changed after it was applied.")
                continue
            conn.execute(sql)
            conn.execute(
                "INSERT INTO proof_schema_migration (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            applied.append(version)
        conn.commit()
    return applied


def _record_legacy_migration_aliases(conn) -> None:
    for legacy_version, current_version in LEGACY_MIGRATION_ALIASES.items():
        legacy = conn.execute(
            "SELECT 1 FROM proof_schema_migration WHERE version = %s",
            (legacy_version,),
        ).fetchone()
        if not legacy:
            continue

        path = MIGRATIONS_DIR / f"{current_version}.sql"
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        current = conn.execute(
            "SELECT checksum FROM proof_schema_migration WHERE version = %s",
            (current_version,),
        ).fetchone()
        if current:
            if current[0] != checksum:
                raise RuntimeError(f"Migration {current_version} changed after it was applied.")
            continue
        conn.execute(
            "INSERT INTO proof_schema_migration (version, checksum) VALUES (%s, %s)",
            (current_version, checksum),
        )


def main() -> None:
    applied = run_migrations()
    if applied:
        print("Applied migrations: " + ", ".join(applied))
    else:
        print("Database is up to date.")


if __name__ == "__main__":
    main()
