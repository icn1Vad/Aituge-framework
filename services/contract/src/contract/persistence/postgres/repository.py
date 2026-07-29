from __future__ import annotations

import hashlib
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from contract.application.idempotency import canonical_json
from contract.config import Settings
from contract.errors import ConfigurationError, ContractError
from contract.persistence.models import (
    DocumentBlockCreate,
    DocumentCreate,
    ParseGenerationReservation,
    ReviewCreate,
)


class ContractRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise ConfigurationError("CONTRACT_DATABASE_URL is required")
        self.database_url = settings.database_url

    def connect(self):
        return psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=5,
        )

    def health(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT current_database() AS database, current_setting('server_version') AS version"
            ).fetchone()
            migrations = conn.execute(
                "SELECT COUNT(*) AS count FROM contract_schema_migration"
            ).fetchone()
        return {
            "ok": bool(row),
            "database": row["database"] if row else "",
            "postgres_version": row["version"] if row else "",
            "migration_count": int(migrations["count"] if migrations else 0),
        }

    def create_document(self, value: DocumentCreate) -> tuple[dict[str, Any], bool]:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO contract_document (
                  id, tenant_id, user_id, contract_version_id, original_name,
                  content_type, file_type, file_size, content_hash, storage_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    value.document_id,
                    value.tenant_id,
                    value.user_id,
                    value.contract_version_id,
                    value.original_name,
                    value.content_type,
                    value.file_type,
                    value.file_size,
                    value.content_hash,
                    value.storage_path,
                ),
            ).fetchone()
            reused = row is None
            if row is None:
                row = conn.execute(
                    """
                    SELECT *
                    FROM contract_document
                    WHERE tenant_id = %s AND user_id = %s
                      AND contract_version_id = %s AND content_hash = %s
                    FOR UPDATE
                    """,
                    (
                        value.tenant_id,
                        value.user_id,
                        value.contract_version_id,
                        value.content_hash,
                    ),
                ).fetchone()
            if row is None:
                raise ContractError(
                    "INTERNAL_ERROR",
                    "无法创建或读取合同技术文档记录",
                    status_code=500,
                )
            conn.commit()
        return dict(row), reused

    def get_document(
        self,
        document_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM contract_document
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (document_id, tenant_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def create_review(self, value: ReviewCreate) -> tuple[dict[str, Any], bool]:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO contract_review_run (
                  id, tenant_id, user_id, business_task_id, contract_version_id,
                  model_pack_id, document_id, idempotency_key, request_id, request_fingerprint,
                  file_sha256, perspective, our_party_name, contract_type,
                  review_attitude, status, schema_version
                ) VALUES (
                  %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, 'CREATED', %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    value.review_id,
                    value.tenant_id,
                    value.user_id,
                    value.business_task_id,
                    value.contract_version_id,
                    value.model_pack_id,
                    value.document_id,
                    value.idempotency_key,
                    value.request_id,
                    value.request_fingerprint,
                    value.file_sha256,
                    value.perspective,
                    value.our_party_name,
                    value.contract_type,
                    value.review_attitude,
                    value.schema_version,
                ),
            ).fetchone()
            reused = row is None
            if row is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM contract_review_run
                    WHERE tenant_id = %s
                      AND (
                        business_task_id = %s
                        OR (user_id = %s AND idempotency_key = %s)
                      )
                    FOR UPDATE
                    """,
                    (
                        value.tenant_id,
                        value.business_task_id,
                        value.user_id,
                        value.idempotency_key,
                    ),
                ).fetchall()
                review_ids = {item["id"] for item in rows}
                if len(review_ids) != 1:
                    raise self._idempotency_conflict()
                row = rows[0]
                if row["request_fingerprint"] != value.request_fingerprint:
                    raise self._idempotency_conflict(row["id"])
            conn.commit()
        return dict(row), reused

    def get_review(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM contract_review_run
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (review_id, tenant_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def reserve_parse_generation(
        self,
        *,
        generation_id: str,
        document_id: str,
        tenant_id: str,
        parser_version: str,
    ) -> ParseGenerationReservation:
        with self.connect() as conn:
            document = conn.execute(
                """
                SELECT id, content_hash
                FROM contract_document
                WHERE id = %s AND tenant_id = %s
                FOR UPDATE
                """,
                (document_id, tenant_id),
            ).fetchone()
            if document is None:
                raise ContractError("REVIEW_NOT_FOUND", "合同技术文档不存在", status_code=404)

            existing = conn.execute(
                """
                SELECT id, generation_no, status
                FROM contract_parse_generation
                WHERE document_id = %s AND content_hash = %s AND parser_version = %s
                  AND status IN ('SUCCEEDED', 'CREATED', 'RUNNING')
                ORDER BY CASE status WHEN 'SUCCEEDED' THEN 0 ELSE 1 END, generation_no DESC
                LIMIT 1
                FOR UPDATE
                """,
                (document_id, document["content_hash"], parser_version),
            ).fetchone()
            if existing is not None:
                completed = existing["status"] == "SUCCEEDED"
                if completed:
                    conn.execute(
                        "UPDATE contract_document SET active_generation_id = %s WHERE id = %s",
                        (existing["id"], document_id),
                    )
                conn.commit()
                return ParseGenerationReservation(
                    generation_id=existing["id"],
                    generation_no=existing["generation_no"],
                    reused=True,
                    completed=completed,
                )

            next_number = conn.execute(
                """
                SELECT COALESCE(MAX(generation_no), 0) + 1 AS generation_no
                FROM contract_parse_generation
                WHERE document_id = %s
                """,
                (document_id,),
            ).fetchone()["generation_no"]
            conn.execute(
                """
                INSERT INTO contract_parse_generation (
                  id, tenant_id, document_id, generation_no, content_hash,
                  parser_version, status, started_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'CREATED', NULL)
                """,
                (
                    generation_id,
                    tenant_id,
                    document_id,
                    next_number,
                    document["content_hash"],
                    parser_version,
                ),
            )
            conn.commit()
        return ParseGenerationReservation(
            generation_id=generation_id,
            generation_no=next_number,
            reused=False,
            completed=False,
        )

    def get_parse_generation(
        self,
        generation_id: str,
        *,
        document_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM contract_parse_generation
                WHERE id = %s AND document_id = %s AND tenant_id = %s
                """,
                (generation_id, document_id, tenant_id),
            ).fetchone()
        return dict(row) if row else None

    def stage_parse_generation(
        self,
        *,
        generation_id: str,
        document_id: str,
        tenant_id: str,
        blocks: Iterable[DocumentBlockCreate],
        structural_ir: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        block_values = list(blocks)
        self._validate_blocks(block_values)
        if not structural_ir:
            raise ValueError("structural_ir must be a non-empty object")
        ir_hash = "sha256:" + hashlib.sha256(canonical_json(structural_ir).encode("utf-8")).hexdigest()

        with self.connect() as conn:
            generation = conn.execute(
                """
                SELECT *
                FROM contract_parse_generation
                WHERE id = %s AND document_id = %s AND tenant_id = %s
                FOR UPDATE
                """,
                (generation_id, document_id, tenant_id),
            ).fetchone()
            if generation is None:
                raise ContractError("REVIEW_NOT_FOUND", "解析Generation不存在", status_code=404)
            if generation["status"] == "SUCCEEDED":
                return dict(generation), True
            if generation["status"] not in {"CREATED", "RUNNING"}:
                raise ContractError(
                    "IDEMPOTENCY_CONFLICT",
                    "失败的解析Generation不能写入解析草稿",
                    status_code=409,
                )
            if generation["ir_hash"] == ir_hash and generation["block_count"] == len(block_values):
                saved_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM contract_document_block WHERE generation_id = %s",
                    (generation_id,),
                ).fetchone()["count"]
                if saved_count == len(block_values):
                    conn.commit()
                    return dict(generation), True

            self._replace_blocks(
                conn,
                tenant_id=tenant_id,
                generation_id=generation_id,
                blocks=block_values,
            )
            staged = conn.execute(
                """
                UPDATE contract_parse_generation
                SET status = 'RUNNING', block_count = %s,
                    contract_ir_json = %s, ir_hash = %s,
                    started_at = COALESCE(started_at, now()),
                    completed_at = NULL, error_code = NULL
                WHERE id = %s
                RETURNING *
                """,
                (len(block_values), Jsonb(structural_ir), ir_hash, generation_id),
            ).fetchone()
            conn.commit()
        return dict(staged), False

    def complete_parse_generation(
        self,
        *,
        generation_id: str,
        document_id: str,
        tenant_id: str,
        blocks: Iterable[DocumentBlockCreate],
        contract_ir: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        block_values = list(blocks)
        self._validate_blocks(block_values)
        if not contract_ir:
            raise ValueError("contract_ir must be a non-empty object")
        ir_hash = "sha256:" + hashlib.sha256(canonical_json(contract_ir).encode("utf-8")).hexdigest()

        with self.connect() as conn:
            generation = conn.execute(
                """
                SELECT *
                FROM contract_parse_generation
                WHERE id = %s AND document_id = %s AND tenant_id = %s
                FOR UPDATE
                """,
                (generation_id, document_id, tenant_id),
            ).fetchone()
            if generation is None:
                raise ContractError("REVIEW_NOT_FOUND", "解析Generation不存在", status_code=404)
            if generation["status"] == "SUCCEEDED":
                if generation["ir_hash"] != ir_hash or generation["block_count"] != len(block_values):
                    raise ContractError(
                        "IDEMPOTENCY_CONFLICT",
                        "解析Generation已由不同结果完成",
                        status_code=409,
                    )
                conn.execute(
                    "UPDATE contract_document SET active_generation_id = %s WHERE id = %s",
                    (generation_id, document_id),
                )
                conn.commit()
                return dict(generation), True
            if generation["status"] not in {"CREATED", "RUNNING"}:
                raise ContractError(
                    "IDEMPOTENCY_CONFLICT",
                    "失败的解析Generation不能被直接完成",
                    status_code=409,
                )

            self._replace_blocks(
                conn,
                tenant_id=tenant_id,
                generation_id=generation_id,
                blocks=block_values,
            )
            completed = conn.execute(
                """
                UPDATE contract_parse_generation
                SET status = 'SUCCEEDED', block_count = %s,
                    contract_ir_json = %s, ir_hash = %s,
                    completed_at = now(), error_code = NULL
                WHERE id = %s
                RETURNING *
                """,
                (len(block_values), Jsonb(contract_ir), ir_hash, generation_id),
            ).fetchone()
            conn.execute(
                "UPDATE contract_document SET active_generation_id = %s WHERE id = %s",
                (generation_id, document_id),
            )
            conn.commit()
        return dict(completed), False

    def fail_parse_generation(
        self,
        generation_id: str,
        *,
        tenant_id: str,
        error_code: str,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE contract_parse_generation
                SET status = 'FAILED', block_count = 0,
                    contract_ir_json = NULL, ir_hash = NULL,
                    error_code = %s, completed_at = now()
                WHERE id = %s AND tenant_id = %s
                  AND status IN ('CREATED', 'RUNNING')
                RETURNING id
                """,
                (error_code, generation_id, tenant_id),
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM contract_document_block WHERE generation_id = %s",
                    (generation_id,),
                )
            conn.commit()
        return row is not None

    def get_active_generation(
        self,
        document_id: str,
        *,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT generation.*
                FROM contract_document document
                JOIN contract_parse_generation generation
                  ON generation.id = document.active_generation_id
                WHERE document.id = %s AND document.tenant_id = %s
                  AND generation.status = 'SUCCEEDED'
                """,
                (document_id, tenant_id),
            ).fetchone()
        return dict(row) if row else None

    def get_current_generation(
        self,
        document_id: str,
        *,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT generation.*
                FROM contract_parse_generation generation
                JOIN contract_document document ON document.id = generation.document_id
                WHERE generation.document_id = %s
                  AND generation.tenant_id = %s
                  AND generation.status IN ('CREATED', 'RUNNING', 'SUCCEEDED')
                ORDER BY
                  CASE WHEN document.active_generation_id = generation.id THEN 0 ELSE 1 END,
                  generation.generation_no DESC
                LIMIT 1
                """,
                (document_id, tenant_id),
            ).fetchone()
        return dict(row) if row else None

    def list_blocks(
        self,
        generation_id: str,
        *,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT block_id, generation_id, block_no, block_type, page_number,
                       paragraph_no, char_start, char_end, text, heading_path,
                       metadata_json
                FROM contract_document_block
                WHERE generation_id = %s AND tenant_id = %s
                ORDER BY block_no
                """,
                (generation_id, tenant_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_blocks(blocks: list[DocumentBlockCreate]) -> None:
        if not blocks:
            raise ValueError("A successful parse generation requires at least one block")
        block_ids = [block.block_id for block in blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block_id values must be unique")
        block_numbers = [block.block_no for block in blocks]
        if block_numbers != list(range(1, len(blocks) + 1)):
            raise ValueError("block_no values must be consecutive and start at 1")
        for block in blocks:
            if block.char_start < 0 or block.char_end <= block.char_start:
                raise ValueError("Block character range is invalid")
            if block.char_end - block.char_start != len(block.text):
                raise ValueError("Block range length must match its normalized text")

    @staticmethod
    def _replace_blocks(
        conn,
        *,
        tenant_id: str,
        generation_id: str,
        blocks: list[DocumentBlockCreate],
    ) -> None:
        conn.execute(
            "DELETE FROM contract_document_block WHERE generation_id = %s",
            (generation_id,),
        )
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO contract_document_block (
                  block_id, tenant_id, generation_id, block_no, block_type,
                  page_number, paragraph_no, char_start, char_end, text,
                  heading_path, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        block.block_id,
                        tenant_id,
                        generation_id,
                        block.block_no,
                        block.block_type,
                        block.page_number,
                        block.paragraph_no,
                        block.char_start,
                        block.char_end,
                        block.text,
                        Jsonb(block.heading_path),
                        Jsonb(block.metadata),
                    )
                    for block in blocks
                ],
            )

    @staticmethod
    def _idempotency_conflict(review_id: str | None = None) -> ContractError:
        details = {"review_id": review_id} if review_id else None
        return ContractError(
            "IDEMPOTENCY_CONFLICT",
            "幂等键已被不同的合同审查请求使用",
            status_code=409,
            user_action_required=True,
            details=details,
        )
