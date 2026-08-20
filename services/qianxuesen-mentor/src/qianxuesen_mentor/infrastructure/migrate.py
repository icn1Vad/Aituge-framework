from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

from qianxuesen_mentor.config import Settings, get_settings
from qianxuesen_mentor.errors import QianXuesenError


MIGRATIONS = Path(__file__).with_name("migrations")


def run_migrations(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    if not settings.database_url:
        raise QianXuesenError("database_unconfigured", "QXS_DATABASE_URL is required", status_code=503)
    applied: list[str] = []
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qxs_schema_migration (
              version text PRIMARY KEY, checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = path.stem
            sql = path.read_text("utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            row = conn.execute("SELECT checksum FROM qxs_schema_migration WHERE version=%s", (version,)).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f"Migration {version} changed after application")
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO qxs_schema_migration(version, checksum) VALUES (%s,%s)", (version, checksum))
            applied.append(version)
        conn.commit()
    return applied


def main() -> None:
    print("Applied migrations: " + ", ".join(run_migrations()))


if __name__ == "__main__":
    main()
