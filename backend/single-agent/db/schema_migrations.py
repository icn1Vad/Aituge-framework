"""Versioned PostgreSQL schema entrypoint for the framework runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
REVISION_TABLE = "framework_schema_revision"
REVISION_TABLE_SQL = f"public.{REVISION_TABLE}"
_CREATE_TABLE = re.compile(r"CREATE TABLE public\.([a-zA-Z0-9_]+)", re.IGNORECASE)


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def migration_manifest() -> list[tuple[str, str, str]]:
    manifest = []
    for path in migration_files():
        sql = path.read_text(encoding="utf-8")
        manifest.append((path.stem.split("_", 1)[0], hashlib.sha256(sql.encode()).hexdigest(), sql))
    if not manifest:
        raise RuntimeError(f"No framework migrations found in {MIGRATIONS_DIR}")
    return manifest


def expected_tables() -> set[str]:
    return {
        table
        for _revision, _checksum, sql in migration_manifest()
        for table in _CREATE_TABLE.findall(sql)
    }


def connection_kwargs() -> dict[str, object]:
    if os.getenv("DB_TYPE") != "postgresql":
        raise RuntimeError("Framework runtime schema requires DB_TYPE=postgresql")
    required = {name: os.getenv(name, "").strip() for name in ("DB_NAME", "DB_USER", "DB_PASSWORD")}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing database settings: {', '.join(missing)}")
    return {
        "dbname": required["DB_NAME"],
        "user": required["DB_USER"],
        "password": required["DB_PASSWORD"],
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "5432")),
    }


def _create_revision_table(cursor) -> None:
    cursor.execute(
        f"""CREATE TABLE IF NOT EXISTS {REVISION_TABLE_SQL} (
            revision VARCHAR(32) PRIMARY KEY,
            checksum VARCHAR(64) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def apply_migrations() -> dict[str, object]:
    manifest = migration_manifest()
    with psycopg.connect(**connection_kwargs()) as conn:
        with conn.cursor() as cursor:
            _create_revision_table(cursor)
            cursor.execute(f"SELECT revision, checksum FROM {REVISION_TABLE_SQL} ORDER BY revision")
            applied = dict(cursor.fetchall())
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> %s",
                (REVISION_TABLE,),
            )
            existing = {row[0] for row in cursor.fetchall()}
            if existing and not applied:
                raise RuntimeError("Framework database has tables but no revision record; use an empty database")
            for revision, checksum, sql in manifest:
                if revision in applied:
                    if applied[revision] != checksum:
                        raise RuntimeError(f"Migration {revision} checksum changed")
                    continue
                cursor.execute(sql)
                cursor.execute(
                    f"INSERT INTO {REVISION_TABLE_SQL} (revision, checksum) VALUES (%s, %s)",
                    (revision, checksum),
                )
        conn.commit()
    return inspect_schema()


def inspect_schema() -> dict[str, object]:
    manifest = migration_manifest()
    expected_revisions = {revision: checksum for revision, checksum, _sql in manifest}
    expected = expected_tables()
    with psycopg.connect(**connection_kwargs()) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (f"public.{REVISION_TABLE}",))
        if cursor.fetchone()[0] is None:
            applied = {}
        else:
            cursor.execute(f"SELECT revision, checksum FROM {REVISION_TABLE_SQL} ORDER BY revision")
            applied = dict(cursor.fetchall())
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        actual = {row[0] for row in cursor.fetchall()}
    domain_actual = actual - {REVISION_TABLE}
    return {
        "database": connection_kwargs()["dbname"],
        "revisions": sorted(applied),
        "pending_revisions": sorted(set(expected_revisions) - set(applied)),
        "checksum_mismatches": sorted(
            revision for revision in set(expected_revisions) & set(applied)
            if expected_revisions[revision] != applied[revision]
        ),
        "expected_table_count": len(expected),
        "actual_table_count": len(domain_actual),
        "missing_tables": sorted(expected - domain_actual),
        "unknown_tables": sorted(domain_actual - expected),
    }


def assert_schema_current(result: dict[str, object]) -> None:
    failures = (
        result["pending_revisions"],
        result["checksum_mismatches"],
        result["missing_tables"],
        result["unknown_tables"],
    )
    if any(failures):
        raise RuntimeError(f"Framework database schema is not current: {json.dumps(result, ensure_ascii=False)}")


async def verify_runtime_schema(engine: AsyncEngine) -> None:
    if engine.url.get_backend_name() != "postgresql":
        return
    manifest = migration_manifest()
    expected_revisions = {revision: checksum for revision, checksum, _sql in manifest}
    async with engine.connect() as conn:
        exists = await conn.scalar(text("SELECT to_regclass(:name)"), {"name": f"public.{REVISION_TABLE}"})
        if exists is None:
            raise RuntimeError("Framework database is not initialized; run source.sh db bootstrap")
        rows = await conn.execute(text(f"SELECT revision, checksum FROM {REVISION_TABLE_SQL}"))
        applied = dict(rows.fetchall())
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
        actual = {row[0] for row in rows.fetchall()} - {REVISION_TABLE}
    if applied != expected_revisions or actual != expected_tables():
        raise RuntimeError("Framework database schema is not current; run source.sh db check")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "check", "status"))
    args = parser.parse_args()
    result = apply_migrations() if args.action == "apply" else inspect_schema()
    if args.action == "check":
        assert_schema_current(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
