from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


class MySqlLegalSource:
    """Read-only, cursor-paged Adapter over Java's ``biz_legal_*`` tables."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        host: str | None = None,
        port: int = 3306,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        charset: str = "utf8mb4",
    ) -> None:
        self.database_url = database_url
        self.connection_options = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
            "charset": charset,
        }
        if not database_url and not all((host, user, database)):
            raise ValueError("MySQL URL or host/user/database settings are required")

    def _connect(self):
        # Kept lazy so ai-contract runtime does not require MySQL connectivity;
        # only the one-shot indexer imports this optional dependency.
        import pymysql
        from pymysql.cursors import SSDictCursor

        if self.database_url:
            parsed = urlparse(self.database_url)
            if parsed.scheme not in {"mysql", "mysql+pymysql"}:
                raise ValueError("MySQL URL must use mysql:// or mysql+pymysql://")
            query = parse_qs(parsed.query)
            options = {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": parsed.path.lstrip("/"),
                "charset": query.get("charset", ["utf8mb4"])[0],
            }
        else:
            options = dict(self.connection_options)
        connection = pymysql.connect(
            **options,
            cursorclass=SSDictCursor,
            autocommit=True,
            read_timeout=120,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        return connection

    def active_release(self) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT release_id, schema_version, parser_version, source_dataset,
                       manifest_sha256, status
                FROM biz_legal_release
                WHERE status = 'ACTIVE'
                ORDER BY activated_at DESC, imported_at DESC
                LIMIT 1
                """
            )
            return cursor.fetchone()

    def retrieval_root_count(self, release_id: str) -> int:
        """Return the exact number of ARTICLE/PREAMBLE units expected downstream."""
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) AS unit_count
                FROM biz_legal_node
                WHERE release_id = %s AND node_type IN ('ARTICLE', 'PREAMBLE')
                """,
                (release_id,),
            )
            row = cursor.fetchone()
        return int((row or {}).get("unit_count") or 0)

    def iter_nodes(
        self,
        release_id: str,
        *,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        last_version_id = ""
        last_sequence = -1
        with self._connect() as conn, conn.cursor() as cursor:
            while True:
                cursor.execute(
                    """
                    SELECT n.release_id, i.instrument_key, v.version_id,
                           i.title, i.category_root, i.jurisdiction_code, i.jurisdiction_name,
                           i.issuing_authority_names_json, v.source_url,
                           n.node_id, n.parent_node_id, n.node_type,
                           n.node_number, n.heading, n.content_plain,
                           n.sequence, n.path, n.content_sha256
                    FROM biz_legal_node n
                    JOIN biz_legal_version v
                      ON v.release_id = n.release_id AND v.version_id = n.version_id
                    JOIN biz_legal_instrument i
                      ON i.release_id = v.release_id AND i.instrument_key = v.instrument_key
                    WHERE n.release_id = %s
                      AND (n.version_id > %s OR (n.version_id = %s AND n.sequence > %s))
                    ORDER BY n.version_id, n.sequence
                    LIMIT %s
                    """,
                    (
                        release_id,
                        last_version_id,
                        last_version_id,
                        last_sequence,
                        page_size,
                    ),
                )
                rows = list(cursor.fetchall())
                if not rows:
                    return
                yield from rows
                last_version_id = str(rows[-1]["version_id"])
                last_sequence = int(rows[-1]["sequence"])

    def iter_relations(
        self,
        release_id: str,
        *,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        last_relation_id = ""
        with self._connect() as conn, conn.cursor() as cursor:
            while True:
                cursor.execute(
                    """
                    SELECT relation_id, release_id, source_instrument_key,
                           source_version_id, target_instrument_key,
                           target_version_id, relation_type, evidence_text,
                           evidence_location_json, confidence
                    FROM biz_legal_relation
                    WHERE release_id = %s AND relation_id > %s
                    ORDER BY relation_id
                    LIMIT %s
                    """,
                    (release_id, last_relation_id, page_size),
                )
                rows = list(cursor.fetchall())
                if not rows:
                    return
                yield from rows
                last_relation_id = str(rows[-1]["relation_id"])

    def iter_instruments(
        self,
        release_id: str,
        *,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Stream instrument aliases for deterministic named-reference linking."""
        last_instrument_key = ""
        with self._connect() as conn, conn.cursor() as cursor:
            while True:
                cursor.execute(
                    """
                    SELECT instrument_key, title, normalized_title, raw_titles_json
                    FROM biz_legal_instrument
                    WHERE release_id = %s AND instrument_key > %s
                    ORDER BY instrument_key
                    LIMIT %s
                    """,
                    (release_id, last_instrument_key, page_size),
                )
                rows = list(cursor.fetchall())
                if not rows:
                    return
                yield from rows
                last_instrument_key = str(rows[-1]["instrument_key"])
