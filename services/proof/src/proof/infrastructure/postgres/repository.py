from __future__ import annotations

from collections.abc import Iterable
from time import perf_counter
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from proof.config import Settings
from proof.domain import DocumentBlock, RetrievalUnit
from proof.errors import ConfigurationError, ProofError
from proof.infrastructure.embedding import EmbeddingProfile


def _vector_text(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".12g") for value in vector) + "]"


def _retrieval_filters(
    *,
    policy_ids: list[str],
    level_codes: list[str],
    category_codes: list[str],
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    for column, values in (
        ("p.id", policy_ids),
        ("p.level_code", level_codes),
        ("p.category_code", category_codes),
    ):
        if values:
            where.append(f"{column} = ANY(%s)")
            params.append(values)
    return where, params


class ProofRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise ConfigurationError("PROOF_DATABASE_URL is required.")
        self.database_url = settings.database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def health(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT current_database() AS database,
                       (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS vector_version,
                       (SELECT extversion FROM pg_extension WHERE extname = 'pg_jieba') AS jieba_version,
                       (SELECT cfgname FROM pg_ts_config WHERE cfgname = 'jiebacfg') AS text_search_config
                """
            ).fetchone()
            migrations = conn.execute("SELECT COUNT(*) AS count FROM proof_schema_migration").fetchone()
        return {
            "ok": bool(row and row["vector_version"]),
            "database": row["database"] if row else "",
            "pgvector_version": row["vector_version"] if row else "",
            "full_text_search": {
                "ok": bool(row and row["text_search_config"]),
                "extension": "pg_jieba",
                "extension_version": row["jieba_version"] if row else "",
                "config": row["text_search_config"] if row else "",
            },
            "migration_count": int(migrations["count"] if migrations else 0),
        }

    def create_ingestion_run(
        self,
        *,
        run_id: str,
        content_hash: str,
        original_name: str,
        parser_version: str,
        clause_profile: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO proof_ingestion_run (
                  id, content_hash, original_name, parser_version, clause_profile
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (run_id, content_hash, original_name, parser_version, clause_profile),
            )
            conn.commit()

    def update_ingestion_run(
        self,
        run_id: str,
        *,
        stage: str,
        block_count: int | None = None,
        clause_count: int | None = None,
        warning_count: int | None = None,
        clause_profile: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_ingestion_run
                SET stage = %s,
                    block_count = COALESCE(%s, block_count),
                    clause_count = COALESCE(%s, clause_count),
                    warning_count = COALESCE(%s, warning_count),
                    clause_profile = COALESCE(%s, clause_profile)
                WHERE id = %s AND status = 'running'
                """,
                (stage, block_count, clause_count, warning_count, clause_profile, run_id),
            )
            conn.commit()

    def complete_ingestion_run(
        self,
        run_id: str,
        *,
        policy_id: str,
        document_id: str,
        reused: bool,
        block_count: int,
        clause_count: int,
        warning_count: int,
        clause_profile: str | None = None,
    ) -> None:
        with self.connect() as conn:
            self._complete_ingestion_run_on_connection(
                conn,
                run_id=run_id,
                policy_id=policy_id,
                document_id=document_id,
                reused=reused,
                block_count=block_count,
                clause_count=clause_count,
                warning_count=warning_count,
                clause_profile=clause_profile,
            )
            conn.commit()

    def fail_ingestion_run(
        self,
        run_id: str,
        *,
        stage: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_ingestion_run
                SET status = 'failed', stage = %s, error_code = %s,
                    error_message = %s, error_details = %s,
                    finished_at = now()
                WHERE id = %s AND status = 'running'
                """,
                (stage, error_code, error_message, Jsonb(error_details), run_id),
            )
            conn.commit()

    def get_ingestion_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, content_hash, original_name, policy_id, document_id,
                       parser_version, clause_profile, status, stage, reused,
                       block_count, clause_count, warning_count, error_code,
                       error_message, error_details, started_at, finished_at
                FROM proof_ingestion_run
                WHERE id = %s
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_document_counts(self, document_id: str) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM proof_document_block WHERE document_id = %s)::integer AS block_count,
                  (SELECT COUNT(*) FROM proof_retrieval_unit WHERE document_id = %s)::integer AS clause_count
                """,
                (document_id, document_id),
            ).fetchone()
        return dict(row) if row else {"block_count": 0, "clause_count": 0}

    def get_documents_by_content_hashes(self, content_hashes: list[str]) -> dict[str, dict[str, Any]]:
        if not content_hashes:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.content_hash, d.id AS document_id, d.policy_id,
                       d.status AS document_status, p.title, p.level_code,
                       p.category_code,
                       d.structure_profile, d.structure_diagnostics,
                       d.parser_version, d.chunker_version
                FROM proof_document d
                JOIN proof_policy p ON p.id = d.policy_id
                WHERE d.content_hash = ANY(%s)
                """,
                (content_hashes,),
            ).fetchall()
        return {row["content_hash"]: dict(row) for row in rows}

    def get_structure_units_by_document_ids(
        self,
        document_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not document_ids:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT document_id, clause_ordinal, clause_no_raw, unit_type,
                       text_hash, heading_path, source_block_ids,
                       page_start, page_end, paragraph_start, paragraph_end
                FROM proof_retrieval_unit
                WHERE document_id = ANY(%s)
                ORDER BY document_id, clause_ordinal
                """,
                (document_ids,),
            ).fetchall()
        result = {document_id: [] for document_id in document_ids}
        for row in rows:
            result.setdefault(row["document_id"], []).append(dict(row))
        return result

    def list_levels(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return list(conn.execute("SELECT code, name, sort_rank FROM proof_policy_level ORDER BY sort_rank DESC"))

    def list_categories(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT code, name, description, parent_code, level
                    FROM proof_category
                    ORDER BY level, parent_code NULLS FIRST, created_at, code
                    """
                )
            )

    def create_category(self, code: str, name: str, description: str) -> dict[str, Any]:
        try:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO proof_category (code, name, description)
                    VALUES (%s, %s, %s)
                    RETURNING code, name, description
                    """,
                    (code, name, description),
                ).fetchone()
                conn.commit()
                return dict(row)
        except psycopg.errors.UniqueViolation as exc:
            raise ProofError("category_exists", f"Category '{code}' already exists.", status_code=409) from exc

    def get_by_content_hash(self, content_hash: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, d.id AS document_id, d.content_hash, d.original_name, d.file_type,
                       d.storage_path, d.status AS document_status, d.parser_version,
                       d.chunker_version, d.parse_warnings, d.structure_profile,
                       d.structure_diagnostics
                FROM proof_document d
                JOIN proof_policy p ON p.id = d.policy_id
                WHERE d.content_hash = %s
                """,
                (content_hash,),
            ).fetchone()
            if not row:
                return None
            clauses = self._clauses_for_document(conn, row["document_id"], include_text=False)
            return self._ingestion_payload(row, clauses)

    def ingest(
        self,
        *,
        policy_id: str,
        document_id: str,
        title: str,
        normalized_title: str,
        version: str,
        level_code: str | None,
        category_code: str,
        content_hash: str,
        original_name: str,
        file_type: str,
        storage_path: str,
        parser_version: str,
        chunker_version: str,
        warnings: list[str],
        structure_profile: str,
        structure_diagnostics: dict[str, Any],
        blocks: list[DocumentBlock],
        units: list[RetrievalUnit],
        ingestion_run_id: str | None = None,
        ingestion_warning_count: int | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            self._validate_metadata(conn, level_code, category_code)
            conn.execute(
                """
                INSERT INTO proof_policy (
                  id, title, normalized_title, version, status, level_code, category_code
                ) VALUES (%s, %s, %s, %s, 'draft', %s, %s)
                """,
                (policy_id, title, normalized_title, version, level_code, category_code),
            )
            conn.execute(
                """
                INSERT INTO proof_document (
                  id, policy_id, content_hash, original_name, file_type, storage_path,
                  status, parser_version, chunker_version, parse_warnings,
                  structure_profile, structure_diagnostics
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending_embedding', %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    policy_id,
                    content_hash,
                    original_name,
                    file_type,
                    storage_path,
                    parser_version,
                    chunker_version,
                    Jsonb(warnings),
                    structure_profile,
                    Jsonb(structure_diagnostics),
                ),
            )
            self._insert_blocks(conn, document_id, blocks)
            self._insert_units(conn, document_id, policy_id, units)
            if ingestion_run_id:
                self._complete_ingestion_run_on_connection(
                    conn,
                    run_id=ingestion_run_id,
                    policy_id=policy_id,
                    document_id=document_id,
                    reused=False,
                    block_count=len(blocks),
                    clause_count=len(units),
                    warning_count=(
                        ingestion_warning_count if ingestion_warning_count is not None else len(warnings)
                    ),
                    clause_profile=structure_profile,
                )
            conn.commit()
        payload = self.get_by_content_hash(content_hash)
        if payload is None:
            raise RuntimeError("Ingested document could not be read back.")
        return payload

    def rebuild_document_structure(
        self,
        *,
        content_hash: str,
        parser_version: str,
        chunker_version: str,
        warnings: list[str],
        structure_profile: str,
        structure_diagnostics: dict[str, Any],
        blocks: list[DocumentBlock],
        units: list[RetrievalUnit],
    ) -> dict[str, Any] | None:
        """Atomically replace derived blocks/units while keeping policy identity and source file."""
        with self.connect() as conn:
            document = conn.execute(
                "SELECT id, policy_id FROM proof_document WHERE content_hash = %s FOR UPDATE",
                (content_hash,),
            ).fetchone()
            if not document:
                return None
            document_id = document["id"]
            policy_id = document["policy_id"]
            conn.execute("DELETE FROM proof_retrieval_unit WHERE document_id = %s", (document_id,))
            conn.execute("DELETE FROM proof_document_block WHERE document_id = %s", (document_id,))
            self._insert_blocks(conn, document_id, blocks)
            self._insert_units(conn, document_id, policy_id, units)
            conn.execute(
                """
                UPDATE proof_document
                SET parser_version = %s, chunker_version = %s, parse_warnings = %s,
                    structure_profile = %s, structure_diagnostics = %s,
                    status = 'pending_embedding', embedding_error_code = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    parser_version,
                    chunker_version,
                    Jsonb(warnings),
                    structure_profile,
                    Jsonb(structure_diagnostics),
                    document_id,
                ),
            )
            conn.commit()
        return {
            "document_id": document_id,
            "policy_id": policy_id,
            "structure_profile": structure_profile,
            "block_count": len(blocks),
            "clause_count": len(units),
        }

    @staticmethod
    def _insert_blocks(conn, document_id: str, blocks: list[DocumentBlock]) -> None:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO proof_document_block (
                  id, document_id, ordinal, block_type, text, page_no, paragraph_index,
                  heading_path, char_start, char_end, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        block.id,
                        document_id,
                        block.ordinal,
                        block.block_type,
                        block.text,
                        block.page_no,
                        block.paragraph_index,
                        Jsonb(block.heading_path),
                        block.char_start,
                        block.char_end,
                        Jsonb(block.metadata),
                    )
                    for block in blocks
                ],
            )

    @staticmethod
    def _insert_units(conn, document_id: str, policy_id: str, units: list[RetrievalUnit]) -> None:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO proof_retrieval_unit (
                  id, document_id, policy_id, clause_no_raw, clause_ordinal, text,
                  heading_path, source_block_ids, page_start, page_end,
                  paragraph_start, paragraph_end, char_start, char_end, text_hash,
                  unit_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        unit.id,
                        document_id,
                        policy_id,
                        unit.clause_no_raw,
                        unit.clause_ordinal,
                        unit.text,
                        Jsonb(unit.heading_path),
                        Jsonb(unit.source_block_ids),
                        unit.page_start,
                        unit.page_end,
                        unit.paragraph_start,
                        unit.paragraph_end,
                        unit.char_start,
                        unit.char_end,
                        unit.text_hash,
                        unit.unit_type,
                    )
                    for unit in units
                ],
            )

    @staticmethod
    def _complete_ingestion_run_on_connection(
        conn,
        *,
        run_id: str,
        policy_id: str,
        document_id: str,
        reused: bool,
        block_count: int,
        clause_count: int,
        warning_count: int,
        clause_profile: str | None = None,
    ) -> None:
        conn.execute(
            """
            UPDATE proof_ingestion_run
            SET policy_id = %s, document_id = %s, status = 'succeeded',
                stage = 'complete', reused = %s, block_count = %s,
                clause_count = %s, warning_count = %s,
                error_code = NULL, error_message = NULL,
                error_details = '{}'::jsonb, finished_at = now(),
                clause_profile = COALESCE(%s, clause_profile)
            WHERE id = %s AND status = 'running'
            """,
            (
                policy_id,
                document_id,
                reused,
                block_count,
                clause_count,
                warning_count,
                clause_profile,
                run_id,
            ),
        )

    def _validate_metadata(self, conn, level_code: str | None, category_code: str) -> None:
        if level_code:
            exists = conn.execute("SELECT 1 FROM proof_policy_level WHERE code = %s", (level_code,)).fetchone()
            if not exists:
                raise ProofError("invalid_policy_level", f"Unknown policy level: {level_code}", status_code=422)
        exists = conn.execute("SELECT 1 FROM proof_category WHERE code = %s", (category_code,)).fetchone()
        if not exists:
            raise ProofError("invalid_category", f"Unknown category: {category_code}", status_code=422)

    def list_policies(
        self,
        *,
        level_code: str | None = None,
        category_code: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("p.level_code", level_code),
            ("p.category_code", category_code),
        ):
            if value:
                where.append(f"{column} = %s")
                params.append(value)
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT p.id, p.title, p.normalized_title, p.version, p.status,
                       p.level_code, p.category_code,
                       p.created_at, p.updated_at,
                       l.name AS level_name, c.name AS category_name,
                       d.id AS document_id, d.status AS document_status,
                       d.structure_profile, d.structure_diagnostics,
                       COUNT(u.id)::integer AS clause_count
                FROM proof_policy p
                LEFT JOIN proof_policy_level l ON l.code = p.level_code
                JOIN proof_category c ON c.code = p.category_code
                JOIN proof_document d ON d.policy_id = p.id
                LEFT JOIN proof_retrieval_unit u ON u.policy_id = p.id
                {clause}
                GROUP BY p.id, l.name, c.name, d.id, d.status
                ORDER BY p.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.id, p.title, p.normalized_title, p.version, p.status,
                       p.level_code, p.category_code,
                       p.created_at, p.updated_at,
                       l.name AS level_name, c.name AS category_name,
                       d.id AS document_id, d.content_hash, d.original_name,
                       d.file_type, d.storage_path, d.status AS document_status,
                       d.parser_version, d.chunker_version, d.parse_warnings,
                       d.structure_profile, d.structure_diagnostics,
                       COUNT(u.id)::integer AS clause_count
                FROM proof_policy p
                LEFT JOIN proof_policy_level l ON l.code = p.level_code
                JOIN proof_category c ON c.code = p.category_code
                JOIN proof_document d ON d.policy_id = p.id
                LEFT JOIN proof_retrieval_unit u ON u.policy_id = p.id
                WHERE p.id = %s
                GROUP BY p.id, l.name, c.name, d.id
                """,
                (policy_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_policy_by_document_id(self, document_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.id, p.status, d.id AS document_id
                FROM proof_policy p
                JOIN proof_document d ON d.policy_id = p.id
                WHERE d.id = %s
                """,
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_conflict_source_unit(self, unit_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.document_id, u.policy_id, u.clause_no_raw,
                       u.clause_ordinal, u.unit_type, u.text, u.heading_path,
                       u.page_start, u.page_end, u.text_hash,
                       p.title AS policy_title, p.normalized_title,
                       p.version AS policy_version, p.status AS policy_status,
                       p.level_code, p.category_code,
                       c.name AS category_name, c.parent_code AS parent_category_code,
                       c.level AS category_level, d.original_name
                FROM proof_retrieval_unit u
                JOIN proof_policy p ON p.id = u.policy_id
                JOIN proof_document d ON d.id = u.document_id
                JOIN proof_category c ON c.code = p.category_code
                WHERE u.id = %s
                """,
                (unit_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_unique_near_unit_id(self, unit_id: str, *, max_distance: int = 1) -> str | None:
        """Resolve a single-character LLM copy error without guessing among multiple units."""

        if not unit_id or max_distance < 1:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM proof_retrieval_unit
                WHERE char_length(id) = %s
                ORDER BY id
                """,
                (len(unit_id),),
            ).fetchall()
        matches = [
            str(row["id"])
            for row in rows
            if sum(left != right for left, right in zip(unit_id, str(row["id"]), strict=True))
            <= max_distance
        ]
        return matches[0] if len(matches) == 1 else None

    def find_effective_policy_ids_by_normalized_title(
        self,
        normalized_title: str,
        *,
        exclude_policy_id: str,
    ) -> list[str]:
        if not normalized_title:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM proof_policy
                WHERE status = 'effective'
                  AND normalized_title = %s
                  AND id <> %s
                ORDER BY id
                """,
                (normalized_title, exclude_policy_id),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def get_category_context(self, category_code: str) -> dict[str, Any] | None:
        if not category_code:
            return None
        with self.connect() as conn:
            category = conn.execute(
                """
                SELECT code, name, parent_code, level
                FROM proof_category
                WHERE code = %s
                """,
                (category_code,),
            ).fetchone()
            if category is None:
                return None
            parent_code = category["parent_code"] if int(category["level"]) == 2 else category["code"]
            children = conn.execute(
                """
                SELECT code
                FROM proof_category
                WHERE parent_code = %s
                ORDER BY code
                """,
                (parent_code,),
            ).fetchall()
        child_codes = [str(row["code"]) for row in children]
        return {
            **dict(category),
            "parent_category_codes": child_codes if int(category["level"]) == 2 else [],
            "child_category_codes": child_codes if int(category["level"]) == 1 else [],
        }

    def list_policy_metadata(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, normalized_title, category_code
                FROM proof_policy
                ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_policy_metadata(
        self,
        policy_id: str,
        *,
        normalized_title: str,
        category_code: str | None = None,
    ) -> bool:
        with self.connect() as conn:
            if category_code is not None:
                self._validate_metadata(conn, None, category_code)
            cursor = conn.execute(
                """
                UPDATE proof_policy
                SET normalized_title = %s,
                    category_code = COALESCE(%s::text, category_code),
                    updated_at = CASE
                      WHEN normalized_title IS DISTINCT FROM %s
                        OR (%s::text IS NOT NULL AND category_code IS DISTINCT FROM %s::text)
                      THEN now()
                      ELSE updated_at
                    END
                WHERE id = %s
                  AND (
                    normalized_title IS DISTINCT FROM %s
                    OR (%s::text IS NOT NULL AND category_code IS DISTINCT FROM %s::text)
                  )
                """,
                (
                    normalized_title,
                    category_code,
                    normalized_title,
                    category_code,
                    category_code,
                    policy_id,
                    normalized_title,
                    category_code,
                    category_code,
                ),
            )
            conn.commit()
        return cursor.rowcount > 0

    def list_clauses(self, policy_id: str, *, include_text: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            document = conn.execute("SELECT id FROM proof_document WHERE policy_id = %s", (policy_id,)).fetchone()
            if not document:
                return []
            return self._clauses_for_document(conn, document["id"], include_text=include_text)

    def _clauses_for_document(self, conn, document_id: str, *, include_text: bool) -> list[dict[str, Any]]:
        text_column = ", text" if include_text else ""
        rows = conn.execute(
            f"""
            SELECT id, clause_no_raw, clause_ordinal, unit_type{text_column}, heading_path,
                   source_block_ids, page_start, page_end, paragraph_start,
                   paragraph_end, char_start, char_end, text_hash, embedding_status
            FROM proof_retrieval_unit
            WHERE document_id = %s
            ORDER BY clause_ordinal
            """,
            (document_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def fetch_units(self, unit_ids: list[str]) -> list[dict[str, Any]]:
        if not unit_ids:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT u.*, p.title AS policy_title, p.version AS policy_version,
                       p.level_code, l.name AS level_name,
                       p.category_code, c.name AS category_name, d.original_name
                FROM proof_retrieval_unit u
                JOIN proof_policy p ON p.id = u.policy_id
                JOIN proof_document d ON d.id = u.document_id
                LEFT JOIN proof_policy_level l ON l.code = p.level_code
                JOIN proof_category c ON c.code = p.category_code
                WHERE u.id = ANY(%s) AND p.status = 'effective'
                """,
                (unit_ids,),
            ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[unit_id] for unit_id in unit_ids if unit_id in by_id]

    def get_document_units(self, document_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, text, clause_no_raw, clause_ordinal, heading_path
                    FROM proof_retrieval_unit
                    WHERE document_id = %s
                    ORDER BY clause_ordinal
                    """,
                    (document_id,),
                )
            ]

    def get_audit_run_for_document(self, document_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proof_audit_run WHERE document_id = %s",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_audit_run(self, audit_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proof_audit_run WHERE id = %s",
                (audit_run_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_audit_run(self, *, audit_run_id: str, document_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO proof_audit_run (id, document_id, status)
                VALUES (%s, %s, 'pending')
                ON CONFLICT (document_id) DO NOTHING
                """,
                (audit_run_id, document_id),
            )
            conn.commit()
        run = self.get_audit_run_for_document(document_id)
        if run is None:
            raise RuntimeError("Audit run could not be read back.")
        return run

    def reset_failed_audit_run(self, audit_run_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE proof_audit_run
                SET status = 'pending', framework_task_id = NULL, framework_run_id = NULL,
                    error_message = NULL, summary_status = 'pending', summary_content = NULL,
                    summary_error_message = NULL, conflict_status = 'pending',
                    conflict_error_message = NULL, updated_at = now()
                WHERE id = %s AND (status = 'failed' OR conflict_status = 'failed')
                RETURNING id
                """,
                (audit_run_id,),
            ).fetchone()
            if row:
                conn.execute("DELETE FROM proof_audit_finding WHERE audit_run_id = %s", (audit_run_id,))
                conn.execute(
                    "DELETE FROM proof_conflict_audit_finding WHERE audit_run_id = %s",
                    (audit_run_id,),
                )
            conn.commit()
        return bool(row)

    def mark_audit_running(
        self,
        audit_run_id: str,
        *,
        framework_task_id: str,
        framework_run_id: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_audit_run
                SET status = CASE WHEN status = 'pending' THEN 'running' ELSE status END,
                    framework_task_id = %s, framework_run_id = %s,
                    error_message = CASE WHEN status = 'pending' THEN NULL ELSE error_message END,
                    summary_status = CASE WHEN summary_status = 'pending' THEN 'running' ELSE summary_status END,
                    summary_content = CASE WHEN summary_status = 'pending' THEN NULL ELSE summary_content END,
                    summary_error_message = CASE
                      WHEN summary_status = 'pending' THEN NULL ELSE summary_error_message
                    END,
                    conflict_status = CASE
                      WHEN conflict_status = 'pending' THEN 'running' ELSE conflict_status
                    END,
                    conflict_error_message = CASE
                      WHEN conflict_status = 'pending' THEN NULL ELSE conflict_error_message
                    END,
                    updated_at = now()
                WHERE id = %s
                """,
                (framework_task_id, framework_run_id, audit_run_id),
            )
            conn.commit()

    def mark_audit_task_created(self, audit_run_id: str, framework_task_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_audit_run
                SET framework_task_id = %s, updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (framework_task_id, audit_run_id),
            )
            conn.commit()

    def mark_audit_failed(self, audit_run_id: str, error_message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_audit_run
                SET status = 'failed', error_message = %s, updated_at = now()
                WHERE id = %s AND status <> 'completed'
                """,
                (error_message[:2000], audit_run_id),
            )
            conn.commit()

    def complete_audit_summary(self, audit_run_id: str, content: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_audit_run
                SET summary_status = 'completed', summary_content = %s::jsonb,
                    summary_error_message = NULL, updated_at = now()
                WHERE id = %s
                """,
                (Jsonb(content), audit_run_id),
            )
            conn.commit()

    def mark_audit_summary_failed(self, audit_run_id: str, error_message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_audit_run
                SET summary_status = 'failed', summary_content = NULL,
                    summary_error_message = %s, updated_at = now()
                WHERE id = %s AND summary_status <> 'completed'
                """,
                (error_message[:2000], audit_run_id),
            )
            conn.commit()

    def complete_conflict_audit(
        self,
        audit_run_id: str,
        findings: list[dict[str, Any]],
    ) -> None:
        with self.connect() as conn:
            run = conn.execute(
                "SELECT id FROM proof_audit_run WHERE id = %s FOR UPDATE",
                (audit_run_id,),
            ).fetchone()
            if not run:
                raise ProofError("audit_run_not_found", "Audit run not found.", status_code=404)
            conn.execute(
                "DELETE FROM proof_conflict_audit_finding WHERE audit_run_id = %s",
                (audit_run_id,),
            )
            if findings:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO proof_conflict_audit_finding (
                          audit_run_id, source_unit_id, candidate_ids, conflict_type,
                          problem, suggestion
                        ) VALUES (
                          %s, %s, %s::jsonb, %s, %s, %s
                        )
                        """,
                        [
                            (
                                audit_run_id,
                                item["id"],
                                Jsonb(item["candidate_ids"]),
                                item["conflict_type"],
                                item["problem"],
                                item["suggestion"],
                            )
                            for item in findings
                        ],
                    )
            conn.execute(
                """
                UPDATE proof_audit_run
                SET conflict_status = 'completed', conflict_error_message = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (audit_run_id,),
            )
            conn.commit()

    def mark_conflict_audit_failed(self, audit_run_id: str, error_message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE proof_audit_run
                SET conflict_status = 'failed', conflict_error_message = %s,
                    updated_at = now()
                WHERE id = %s AND conflict_status <> 'completed'
                """,
                (error_message[:2000], audit_run_id),
            )
            conn.commit()

    def list_conflict_audit_findings(self, audit_run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.source_unit_id AS id, f.candidate_ids, f.conflict_type,
                       f.problem, f.suggestion,
                       u.clause_ordinal, u.clause_no_raw
                FROM proof_conflict_audit_finding f
                JOIN proof_retrieval_unit u ON u.id = f.source_unit_id
                WHERE f.audit_run_id = %s
                ORDER BY u.clause_ordinal, f.id
                """,
                (audit_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_audit(self, audit_run_id: str, findings: list[dict[str, str]]) -> None:
        with self.connect() as conn:
            run = conn.execute(
                "SELECT id FROM proof_audit_run WHERE id = %s FOR UPDATE",
                (audit_run_id,),
            ).fetchone()
            if not run:
                raise ProofError("audit_run_not_found", "Audit run not found.", status_code=404)
            conn.execute("DELETE FROM proof_audit_finding WHERE audit_run_id = %s", (audit_run_id,))
            if findings:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO proof_audit_finding (
                          audit_run_id, retrieval_unit_id, category, problem, suggestion
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                audit_run_id,
                                item["id"],
                                item["category"],
                                item["problem"],
                                item["suggestion"],
                            )
                            for item in findings
                        ],
                    )
            conn.execute(
                """
                UPDATE proof_audit_run
                SET status = 'completed', error_message = NULL, updated_at = now()
                WHERE id = %s
                """,
                (audit_run_id,),
            )
            conn.commit()

    def list_audit_findings(self, audit_run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.retrieval_unit_id AS id, f.category, f.problem, f.suggestion,
                       u.clause_ordinal, u.clause_no_raw
                FROM proof_audit_finding f
                JOIN proof_retrieval_unit u ON u.id = f.retrieval_unit_id
                WHERE f.audit_run_id = %s
                ORDER BY u.clause_ordinal
                """,
                (audit_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_policy(self, policy_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM proof_policy WHERE id = %s FOR UPDATE",
                (policy_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "draft":
                conn.execute(
                    "UPDATE proof_policy SET status = 'effective', updated_at = now() WHERE id = %s",
                    (policy_id,),
                )
            conn.commit()
        return self.get_policy(policy_id)

    def delete_draft_policy(self, policy_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.status, d.storage_path
                FROM proof_policy p
                JOIN proof_document d ON d.policy_id = p.id
                WHERE p.id = %s
                FOR UPDATE OF p
                """,
                (policy_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "draft":
                raise ProofError(
                    "policy_not_draft",
                    "Only draft policies can be discarded.",
                    status_code=409,
                )
            conn.execute("DELETE FROM proof_policy WHERE id = %s", (policy_id,))
            conn.commit()
        return dict(row)

    def replace_embeddings(
        self,
        document_id: str,
        units: list[dict[str, Any]],
        vectors: list[list[float]],
        profile: EmbeddingProfile,
        too_long_unit_ids: list[str] | None = None,
    ) -> None:
        with self.connect() as conn:
            for unit, vector in zip(units, vectors, strict=True):
                conn.execute(
                    """
                    INSERT INTO proof_retrieval_embedding (
                      retrieval_unit_id, profile_id, provider, model, dimensions, embedding
                    ) VALUES (%s, %s, %s, %s, %s, %s::vector)
                    ON CONFLICT (retrieval_unit_id) DO UPDATE SET
                      profile_id = EXCLUDED.profile_id,
                      provider = EXCLUDED.provider,
                      model = EXCLUDED.model,
                      dimensions = EXCLUDED.dimensions,
                      embedding = EXCLUDED.embedding,
                      updated_at = now()
                    """,
                    (unit["id"], profile.id, profile.provider, profile.model, profile.dimensions, _vector_text(vector)),
                )
                conn.execute(
                    "UPDATE proof_retrieval_unit SET embedding_status = 'indexed', updated_at = now() WHERE id = %s",
                    (unit["id"],),
                )
            if too_long_unit_ids:
                conn.execute(
                    """
                    UPDATE proof_retrieval_unit
                    SET embedding_status = 'embedding_too_long', updated_at = now()
                    WHERE document_id = %s AND id = ANY(%s)
                    """,
                    (document_id, too_long_unit_ids),
                )
            conn.execute(
                """
                UPDATE proof_document
                SET status = 'indexed', embedding_error_code = %s, updated_at = now()
                WHERE id = %s
                """,
                ("embedding_too_long" if too_long_unit_ids else None, document_id),
            )
            conn.commit()

    def mark_embedding_failed(self, document_id: str, error_code: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE proof_document SET status = 'embedding_failed', embedding_error_code = %s, updated_at = now() WHERE id = %s",
                (error_code, document_id),
            )
            conn.execute(
                """
                UPDATE proof_retrieval_unit
                SET embedding_status = 'failed', updated_at = now()
                WHERE document_id = %s AND embedding_status <> 'embedding_too_long'
                """,
                (document_id,),
            )
            conn.commit()

    def vector_search(
        self,
        *,
        query_vector: list[float],
        profile: EmbeddingProfile,
        top_k: int,
        policy_ids: list[str],
        level_codes: list[str],
        category_codes: list[str],
        excluded_policy_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        where, where_params = _retrieval_filters(
            policy_ids=policy_ids,
            level_codes=level_codes,
            category_codes=category_codes,
        )
        where[0:0] = ["e.profile_id = %s", "e.dimensions = %s"]
        where_params[0:0] = [profile.id, profile.dimensions]
        if excluded_policy_ids:
            where.append("NOT (p.id = ANY(%s))")
            where_params.append(excluded_policy_ids)
        vector = _vector_text(query_vector)
        params: list[Any] = [vector, *where_params, vector, top_k]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT u.id, u.document_id, u.policy_id, u.clause_no_raw,
                       u.clause_ordinal, u.unit_type, u.text, u.heading_path, u.source_block_ids,
                       u.page_start, u.page_end, u.paragraph_start, u.paragraph_end,
                       u.text_hash,
                       p.title AS policy_title, p.version AS policy_version,
                       p.level_code, l.name AS level_name,
                       p.category_code, c.name AS category_name, d.original_name,
                       1 - (e.embedding <=> %s::vector) AS score
                FROM proof_retrieval_embedding e
                JOIN proof_retrieval_unit u ON u.id = e.retrieval_unit_id
                JOIN proof_policy p ON p.id = u.policy_id
                JOIN proof_document d ON d.id = u.document_id
                LEFT JOIN proof_policy_level l ON l.code = p.level_code
                JOIN proof_category c ON c.code = p.category_code
                WHERE p.status = 'effective' AND {" AND ".join(where)}
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def keyword_search(
        self,
        *,
        query: str,
        top_k: int,
        policy_ids: list[str],
        level_codes: list[str],
        category_codes: list[str],
    ) -> list[dict[str, Any]]:
        where, where_params = _retrieval_filters(
            policy_ids=policy_ids,
            level_codes=level_codes,
            category_codes=category_codes,
        )
        where.insert(0, "u.search_vector @@ q.value")
        params: list[Any] = [query, *where_params, top_k]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                WITH query_terms AS (
                  SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', %s))) AS term
                ),
                q AS (
                  SELECT string_agg(quote_literal(term), ' | ')::tsquery AS value
                  FROM query_terms
                )
                SELECT u.id, u.document_id, u.policy_id, u.clause_no_raw,
                       u.clause_ordinal, u.unit_type, u.text, u.heading_path, u.source_block_ids,
                       u.page_start, u.page_end, u.paragraph_start, u.paragraph_end,
                       u.text_hash,
                       p.title AS policy_title, p.version AS policy_version,
                       p.level_code, l.name AS level_name,
                       p.category_code, c.name AS category_name, d.original_name,
                       ts_rank_cd(u.search_vector, q.value, 32) AS score
                FROM proof_retrieval_unit u
                CROSS JOIN q
                JOIN proof_policy p ON p.id = u.policy_id
                JOIN proof_document d ON d.id = u.document_id
                LEFT JOIN proof_policy_level l ON l.code = p.level_code
                JOIN proof_category c ON c.code = p.category_code
                WHERE p.status = 'effective' AND {" AND ".join(where)}
                ORDER BY score DESC, u.policy_id, u.clause_ordinal
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_embeddings(self, profile_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM proof_retrieval_embedding WHERE profile_id = %s",
                (profile_id,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def execute_read_query(
        self,
        sql: str,
        *,
        statement_timeout_ms: int,
        row_limit: int,
    ) -> dict[str, Any]:
        started = perf_counter()
        try:
            with self.connect() as conn:
                conn.execute("BEGIN READ ONLY")
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(statement_timeout_ms),),
                )
                # Do not bind the trusted, integer row limit as a separate
                # parameter here. Psycopg otherwise parses percent signs in
                # the validated user SELECT (for example ILIKE '%采购%') as
                # placeholders before PostgreSQL sees the query.
                result_limit = int(row_limit) + 1
                cursor = conn.execute(
                    f"SELECT * FROM ({sql}) AS proof_user_query LIMIT {result_limit}"
                )
                rows = [dict(row) for row in cursor.fetchall()]
                columns = [column.name for column in cursor.description or []]
        except psycopg.errors.QueryCanceled as exc:
            raise ProofError(
                "sql_timeout",
                "SQL query exceeded the execution time limit.",
                status_code=408,
            ) from exc
        except psycopg.Error as exc:
            raise ProofError(
                "sql_execution_failed",
                "SQL query could not be executed.",
                status_code=422,
                details={
                    "sqlstate": exc.sqlstate,
                    "reason": str(exc).splitlines()[0][:500],
                },
            ) from exc
        return {
            "columns": columns,
            "rows": rows,
            "execution_ms": round((perf_counter() - started) * 1000, 3),
        }

    @staticmethod
    def _ingestion_payload(row: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, Any]:
        policy = {
            key: row.get(key)
            for key in (
                "id",
                "title",
                "version",
                "status",
                "level_code",
                "category_code",
                "normalized_title",
                "created_at",
                "updated_at",
            )
        }
        document = {
            "id": row.get("document_id"),
            "content_hash": row.get("content_hash"),
            "original_name": row.get("original_name"),
            "file_type": row.get("file_type"),
            "storage_path": row.get("storage_path"),
            "status": row.get("document_status"),
            "parser_version": row.get("parser_version"),
            "chunker_version": row.get("chunker_version"),
            "parse_warnings": row.get("parse_warnings") or [],
            "structure_profile": row.get("structure_profile") or "article",
            "structure_diagnostics": row.get("structure_diagnostics") or {},
        }
        return {"policy": policy, "document": document, "clauses": clauses}
