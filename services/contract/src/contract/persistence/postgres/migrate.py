from __future__ import annotations

import hashlib
from pathlib import Path

from contract.config import Settings, get_settings
from contract.errors import ConfigurationError
from contract.persistence.postgres.connections import open_contract_database_connection


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_LOCK_ID = 1_827_001


def run_migrations(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    if not settings.database_url:
        raise ConfigurationError("CONTRACT_DATABASE_URL is required to run migrations")

    applied: list[str] = []
    with open_contract_database_connection(settings) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contract_schema_migration (
              version text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            sql = path.read_text("utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM contract_schema_migration WHERE version = %s",
                (version,),
            ).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f"Migration {version} changed after it was applied")
                continue
            conn.execute(sql)
            conn.execute(
                "INSERT INTO contract_schema_migration (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            applied.append(version)
        conn.commit()
    return applied


def main() -> None:
    applied = run_migrations()
    if applied:
        print("Applied contract migrations: " + ", ".join(applied))
    else:
        print("Contract database is up to date")


if __name__ == "__main__":
    main()
