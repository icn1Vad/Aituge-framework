from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from contract.api.models import ReviewResultData
from contract.application.idempotency import canonical_json
from contract.application.result_hash import compute_result_hash
from contract.callback.models import FrameworkCallback
from contract.config import Settings
from contract.errors import ConfigurationError, ContractError


STAGE_TO_REVIEW_STAGE = {
    "parse_contract": "PARSING",
    "resolve_parties": "PARTY_RESOLUTION",
    "extract_contract_ir": "IR_EXTRACTION",
    "rights_obligations_review": "RIGHTS_OBLIGATIONS",
    "commercial_terms_review": "RISK_REVIEW",
    "liability_termination_review": "RISK_REVIEW",
    "missing_ambiguous_clauses": "RISK_REVIEW",
    "relation_extraction": "RISK_REVIEW",
    "verify_evidence": "EVIDENCE_VERIFICATION",
    "finalize_review": "FINALIZING",
}
REQUIRED_STAGE_IDS = frozenset(STAGE_TO_REVIEW_STAGE)


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    accepted: bool
    duplicate: bool
    ignored_reason: str | None = None


class FrameworkCallbackRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise ConfigurationError("CONTRACT_DATABASE_URL is required")
        self.database_url = settings.database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def get_review_context(self, review_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            review = conn.execute(
                "SELECT * FROM contract_review_run WHERE id = %s",
                (review_id,),
            ).fetchone()
            if review is None:
                raise ContractError("REVIEW_NOT_FOUND", "Contract review does not exist", status_code=404)
            attempt = None
            if review["active_attempt_no"] is not None:
                attempt = conn.execute(
                    """
                    SELECT * FROM contract_framework_attempt
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (review_id, review["active_attempt_no"]),
                ).fetchone()
            generation = conn.execute(
                """
                SELECT generation.*
                FROM contract_parse_generation generation
                JOIN contract_document document ON document.id = generation.document_id
                WHERE generation.document_id = %s
                  AND generation.tenant_id = %s
                  AND generation.status IN ('RUNNING', 'SUCCEEDED')
                ORDER BY
                  CASE WHEN document.active_generation_id = generation.id THEN 0 ELSE 1 END,
                  generation.generation_no DESC
                LIMIT 1
                """,
                (review["document_id"], review["tenant_id"]),
            ).fetchone()
        value = dict(review)
        value["active_attempt"] = dict(attempt) if attempt else None
        value["generation"] = dict(generation) if generation else None
        return value

    def process(self, callback: FrameworkCallback) -> CallbackOutcome:
        payload = callback.model_dump(mode="json")
        with self.connect() as conn:
            review = conn.execute(
                "SELECT * FROM contract_review_run WHERE id = %s FOR UPDATE",
                (callback.review_id,),
            ).fetchone()
            if review is None:
                raise ContractError("REVIEW_NOT_FOUND", "Contract review does not exist", status_code=404)
            attempt = conn.execute(
                """
                SELECT * FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (callback.review_id, callback.attempt_no),
            ).fetchone()
            if attempt is None:
                raise self._mismatch("Framework Attempt does not exist")
            if (
                attempt["framework_task_id"] != callback.framework_task_id
                or attempt["framework_run_id"] != callback.framework_run_id
            ):
                raise self._mismatch("Framework Task or Run does not match the Attempt")

            existing = conn.execute(
                """
                SELECT * FROM contract_review_stage_result
                WHERE review_id = %s AND attempt_no = %s AND callback_id = %s
                """,
                (callback.review_id, callback.attempt_no, callback.callback_id),
            ).fetchone()
            if existing is not None:
                self._validate_duplicate(existing, payload)
                conn.commit()
                return CallbackOutcome(
                    accepted=existing["validation_status"] == "VALIDATED",
                    duplicate=True,
                    ignored_reason=existing["ignored_reason"],
                )
            sequence_owner = conn.execute(
                """
                SELECT callback_id FROM contract_review_stage_result
                WHERE review_id = %s AND attempt_no = %s AND event_sequence = %s
                """,
                (callback.review_id, callback.attempt_no, callback.event_sequence),
            ).fetchone()
            if sequence_owner is not None:
                raise self._mismatch("event_sequence is already owned by another callback")

            same_attempt = review["active_attempt_no"] == callback.attempt_no
            if not same_attempt or attempt["status"] == "ORPHANED":
                self._insert_callback(
                    conn,
                    callback,
                    payload,
                    validation_status="IGNORED",
                    ignored_reason="STALE_ATTEMPT",
                )
                conn.commit()
                return CallbackOutcome(False, False, "STALE_ATTEMPT")

            terminal_conflict = self._terminal_conflict(attempt["status"], callback.callback_type)
            if terminal_conflict:
                self._insert_callback(
                    conn,
                    callback,
                    payload,
                    validation_status="REJECTED",
                    ignored_reason="FRAMEWORK_PROTOCOL_ERROR",
                )
                conn.commit()
                return CallbackOutcome(False, False, "FRAMEWORK_PROTOCOL_ERROR")

            is_current = bool(attempt["is_active"]) or attempt["status"] in {"SUCCEEDED", "FAILED"}
            if not is_current:
                self._insert_callback(
                    conn,
                    callback,
                    payload,
                    validation_status="IGNORED",
                    ignored_reason="REVIEW_TERMINAL",
                )
                conn.commit()
                return CallbackOutcome(False, False, "REVIEW_TERMINAL")

            if callback.callback_type == "STAGE_RESULT":
                if review["status"] != "RUNNING" or attempt["status"] not in {"RUNNING", "SUCCEEDED"}:
                    self._insert_callback(
                        conn,
                        callback,
                        payload,
                        validation_status="IGNORED",
                        ignored_reason="REVIEW_TERMINAL",
                    )
                    conn.commit()
                    return CallbackOutcome(False, False, "REVIEW_TERMINAL")
                self._validate_stage_identity(conn, review, callback.stage_id, payload["result"])
                if callback.stage_id == "extract_contract_ir":
                    self._promote_contract_ir(conn, review, payload["result"]["contract_ir"])
                self._insert_callback(conn, callback, payload, validation_status="VALIDATED")
                self._advance_stage(conn, review, attempt, callback)
                self._finish_if_ready(conn, review, callback.attempt_no)
            elif callback.callback_type == "RUN_SUCCEEDED":
                self._insert_callback(conn, callback, payload, validation_status="VALIDATED")
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET status = 'SUCCEEDED', last_event_sequence = GREATEST(last_event_sequence, %s),
                        last_activity_at = now(), finished_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (callback.event_sequence, callback.review_id, callback.attempt_no),
                )
                self._finish_if_ready(conn, review, callback.attempt_no)
            else:
                self._insert_callback(conn, callback, payload, validation_status="VALIDATED")
                stage = STAGE_TO_REVIEW_STAGE.get(callback.stage_id or "")
                error = payload["error"]
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET status = 'FAILED', current_stage = COALESCE(%s, current_stage),
                        last_event_sequence = GREATEST(last_event_sequence, %s),
                        last_activity_at = now(), finished_at = now(), is_active = false
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (stage, callback.event_sequence, callback.review_id, callback.attempt_no),
                )
                if review["status"] in {"CREATED", "RUNNING"}:
                    conn.execute(
                        """
                        UPDATE contract_review_run
                        SET status = 'FAILED', current_stage = COALESCE(%s, current_stage),
                            error_code = %s, error_message = %s, retryable = %s,
                            user_action_required = %s, error_details_json = %s,
                            completed_at = now(), version = version + 1
                        WHERE id = %s
                        """,
                        (
                            stage,
                            error["code"],
                            error["message"],
                            error["retryable"],
                            error["user_action_required"],
                            Jsonb(error["details"]) if error["details"] is not None else None,
                            callback.review_id,
                        ),
                    )
            conn.commit()
        return CallbackOutcome(True, False)

    @staticmethod
    def _insert_callback(
        conn,
        callback: FrameworkCallback,
        payload: dict[str, Any],
        *,
        validation_status: str,
        ignored_reason: str | None = None,
    ) -> None:
        digest = hashlib.sha256(
            f"{callback.review_id}\0{callback.attempt_no}\0{callback.callback_id}".encode("utf-8")
        ).hexdigest()[:32]
        result = payload.get("result")
        error = payload.get("error")
        conn.execute(
            """
            INSERT INTO contract_review_stage_result (
              id, tenant_id, review_id, attempt_no, framework_task_id, framework_run_id,
              callback_id, callback_type, event_sequence, stage_id, result_type,
              result_json, error_json, validation_status, ignored_reason
            )
            SELECT %s, tenant_id, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM contract_review_run WHERE id = %s
            """,
            (
                f"callback-{digest}",
                callback.review_id,
                callback.attempt_no,
                callback.framework_task_id,
                callback.framework_run_id,
                callback.callback_id,
                callback.callback_type,
                callback.event_sequence,
                callback.stage_id,
                result.get("result_type") if result else None,
                Jsonb(result) if result is not None else None,
                Jsonb(error) if error is not None else None,
                validation_status,
                ignored_reason,
                callback.review_id,
            ),
        )

    @staticmethod
    def _validate_duplicate(existing: dict[str, Any], payload: dict[str, Any]) -> None:
        expected = (
            payload["framework_task_id"],
            payload["framework_run_id"],
            payload["callback_type"],
            payload["event_sequence"],
            payload.get("stage_id"),
            payload.get("result"),
            payload.get("error"),
        )
        actual = (
            existing["framework_task_id"],
            existing["framework_run_id"],
            existing["callback_type"],
            existing["event_sequence"],
            existing["stage_id"],
            existing["result_json"],
            existing["error_json"],
        )
        if actual != expected:
            raise FrameworkCallbackRepository._mismatch(
                "callback_id was reused with a different callback envelope"
            )

    @staticmethod
    def _terminal_conflict(attempt_status: str, callback_type: str) -> bool:
        if callback_type == "RUN_SUCCEEDED":
            return attempt_status == "FAILED"
        if callback_type == "RUN_FAILED":
            return attempt_status == "SUCCEEDED"
        return False

    @staticmethod
    def _advance_stage(conn, review, attempt, callback: FrameworkCallback) -> None:
        stage = STAGE_TO_REVIEW_STAGE[callback.stage_id]
        should_advance = callback.event_sequence >= attempt["last_event_sequence"]
        conn.execute(
            """
            UPDATE contract_framework_attempt
            SET current_stage = CASE WHEN %s THEN %s ELSE current_stage END,
                last_event_sequence = GREATEST(last_event_sequence, %s),
                last_activity_at = now()
            WHERE review_id = %s AND attempt_no = %s
            """,
            (
                should_advance,
                stage,
                callback.event_sequence,
                callback.review_id,
                callback.attempt_no,
            ),
        )
        if should_advance and review["status"] == "RUNNING":
            conn.execute(
                "UPDATE contract_review_run SET current_stage = %s WHERE id = %s",
                (stage, callback.review_id),
            )

    @staticmethod
    def _validate_stage_identity(conn, review, stage_id: str, result: dict[str, Any]) -> None:
        if stage_id == "parse_contract":
            generation = conn.execute(
                """
                SELECT generation.*
                FROM contract_parse_generation generation
                JOIN contract_document document ON document.id = generation.document_id
                WHERE generation.id = %s AND generation.document_id = %s
                  AND generation.tenant_id = %s
                  AND generation.status IN ('RUNNING', 'SUCCEEDED')
                """,
                (result["generation_id"], review["document_id"], review["tenant_id"]),
            ).fetchone()
            if (
                generation is None
                or result["document_id"] != review["document_id"]
                or result["block_count"] != generation["block_count"]
                or result["ir_hash"] != generation["ir_hash"]
            ):
                raise FrameworkCallbackRepository._mismatch(
                    "Parse stage result does not match its persisted Generation"
                )
            return
        if stage_id == "resolve_parties":
            if result["perspective"] != review["perspective"]:
                raise FrameworkCallbackRepository._mismatch(
                    "Resolved party perspective does not match the review"
                )
            FrameworkCallbackRepository._validate_party_sources(conn, review, result)
            return
        if stage_id != "extract_contract_ir":
            return
        party_row = conn.execute(
            """
            SELECT result_json
            FROM contract_review_stage_result
            WHERE review_id = %s AND attempt_no = %s
              AND callback_type = 'STAGE_RESULT' AND stage_id = 'resolve_parties'
              AND validation_status = 'VALIDATED'
            ORDER BY event_sequence DESC, received_at DESC
            LIMIT 1
            """,
            (review["id"], review["active_attempt_no"]),
        ).fetchone()
        if party_row is None:
            raise ContractError(
                "RESULT_INVALID",
                "Contract IR cannot be accepted before party resolution",
                status_code=422,
            )
        party = party_row["result_json"]
        contract_ir = result["contract_ir"]
        names = {item["role"]: item["name"] for item in contract_ir["parties"]}
        if (
            names.get("PARTY_A") != party["party_a"]["name"]
            or names.get("PARTY_B") != party["party_b"]["name"]
            or contract_ir["our_party"] != party["our_party"]
            or contract_ir["counterparty"] != party["counterparty"]
            or contract_ir["contract_type"] != party["contract_type"]
        ):
            raise FrameworkCallbackRepository._mismatch(
                "Contract IR party mapping does not match the resolved parties"
            )

    @staticmethod
    def _promote_contract_ir(conn, review, contract_ir: dict[str, Any]) -> None:
        document = contract_ir["document"]
        if document["document_id"] != review["document_id"]:
            raise FrameworkCallbackRepository._mismatch("Contract IR belongs to another document")
        generation = conn.execute(
            """
            SELECT * FROM contract_parse_generation
            WHERE id = %s AND document_id = %s AND tenant_id = %s
            FOR UPDATE
            """,
            (document["generation_id"], review["document_id"], review["tenant_id"]),
        ).fetchone()
        if generation is None:
            raise FrameworkCallbackRepository._mismatch("Contract IR generation does not exist")
        structural_ir = generation["contract_ir_json"]
        if not isinstance(structural_ir, dict):
            raise ContractError("RESULT_INVALID", "Structural Contract IR is unavailable", status_code=422)
        if (
            document["content_hash"] != generation["content_hash"]
            or document["parser_version"] != generation["parser_version"]
        ):
            raise FrameworkCallbackRepository._mismatch(
                "Contract IR document metadata does not match its parse generation"
            )
        for field in ("document", "clauses", "source_anchors"):
            if contract_ir[field] != structural_ir.get(field):
                raise ContractError(
                    "RESULT_INVALID",
                    f"Contract IR must preserve structural field '{field}'",
                    status_code=422,
                )
        block_count = conn.execute(
            "SELECT COUNT(*) AS count FROM contract_document_block WHERE generation_id = %s",
            (generation["id"],),
        ).fetchone()["count"]
        if block_count <= 0 or block_count != document["block_count"]:
            raise ContractError("RESULT_INVALID", "Contract IR block count is invalid", status_code=422)
        blocks = conn.execute(
            """
            SELECT block_id, page_number, text
            FROM contract_document_block
            WHERE generation_id = %s AND tenant_id = %s
            """,
            (generation["id"], review["tenant_id"]),
        ).fetchall()
        FrameworkCallbackRepository._validate_ir_sources(contract_ir, blocks)
        ir_hash = "sha256:" + hashlib.sha256(canonical_json(contract_ir).encode("utf-8")).hexdigest()
        if generation["status"] == "SUCCEEDED" and generation["ir_hash"] != ir_hash:
            raise FrameworkCallbackRepository._mismatch(
                "Parse generation was already completed by another Contract IR"
            )
        if generation["status"] not in {"RUNNING", "SUCCEEDED"}:
            raise ContractError("RESULT_INVALID", "Contract IR generation is not writable", status_code=422)
        conn.execute(
            """
            UPDATE contract_parse_generation
            SET status = 'SUCCEEDED', contract_ir_json = %s, ir_hash = %s,
                completed_at = COALESCE(completed_at, now()), error_code = NULL
            WHERE id = %s
            """,
            (Jsonb(contract_ir), ir_hash, generation["id"]),
        )
        conn.execute(
            "UPDATE contract_document SET active_generation_id = %s WHERE id = %s",
            (generation["id"], review["document_id"]),
        )

    @staticmethod
    def _validate_party_sources(conn, review, result: dict[str, Any]) -> None:
        generation = conn.execute(
            """
            SELECT (stage.result_json ->> 'generation_id') AS generation_id
            FROM contract_review_stage_result stage
            WHERE stage.review_id = %s AND stage.attempt_no = %s
              AND stage.callback_type = 'STAGE_RESULT' AND stage.stage_id = 'parse_contract'
              AND stage.validation_status = 'VALIDATED'
            ORDER BY stage.event_sequence DESC, stage.received_at DESC
            LIMIT 1
            """,
            (review["id"], review["active_attempt_no"]),
        ).fetchone()
        if generation is None:
            raise ContractError("RESULT_INVALID", "Party resolution requires a parsed contract", status_code=422)
        rows = conn.execute(
            """
            SELECT text FROM contract_document_block
            WHERE generation_id = %s AND tenant_id = %s
            """,
            (generation["generation_id"], review["tenant_id"]),
        ).fetchall()
        document_text = "\n".join(row["text"] for row in rows)
        party_a = result["party_a"]["name"]
        party_b = result["party_b"]["name"]
        if FrameworkCallbackRepository._normalized_text(party_a) == FrameworkCallbackRepository._normalized_text(
            party_b
        ):
            raise ContractError("RESULT_INVALID", "Resolved contract parties must be distinct", status_code=422)
        normalized_document = FrameworkCallbackRepository._normalized_text(document_text)
        missing = [
            name
            for name in (party_a, party_b)
            if FrameworkCallbackRepository._normalized_text(name) not in normalized_document
        ]
        if missing:
            raise ContractError(
                "RESULT_INVALID",
                "Resolved party names must come from the current contract",
                status_code=422,
                details={"missing_party_names": missing},
            )

    @staticmethod
    def _validate_ir_sources(contract_ir: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        blocks = {row["block_id"]: row for row in rows}
        for anchor in FrameworkCallbackRepository._iter_source_anchors(contract_ir):
            block = blocks.get(anchor["block_id"])
            if block is None:
                raise ContractError(
                    "RESULT_INVALID",
                    "Contract IR source anchor references another document",
                    status_code=422,
                )
            start = anchor["char_start"]
            end = anchor["char_end"]
            if end > len(block["text"]) or start >= end:
                raise ContractError(
                    "RESULT_INVALID",
                    "Contract IR source anchor range is outside its block",
                    status_code=422,
                )
            if anchor.get("page_number") != block["page_number"]:
                raise ContractError(
                    "RESULT_INVALID",
                    "Contract IR source anchor page does not match its block",
                    status_code=422,
                )
        role_counts = {
            role: sum(1 for party in contract_ir["parties"] if party["role"] == role)
            for role in ("PARTY_A", "PARTY_B")
        }
        if role_counts != {"PARTY_A": 1, "PARTY_B": 1}:
            raise ContractError(
                "RESULT_INVALID",
                "Semantic Contract IR requires exactly one PARTY_A and one PARTY_B",
                status_code=422,
            )
        for party in contract_ir["parties"]:
            anchored_text = "\n".join(
                blocks[anchor["block_id"]]["text"][anchor["char_start"] : anchor["char_end"]]
                for anchor in party["source_anchors"]
            )
            if FrameworkCallbackRepository._normalized_text(party["name"]) not in (
                FrameworkCallbackRepository._normalized_text(anchored_text)
            ):
                raise ContractError(
                    "RESULT_INVALID",
                    "Contract IR party name is not supported by its source anchors",
                    status_code=422,
                )

    @staticmethod
    def _iter_source_anchors(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "source_anchors" and isinstance(item, list):
                    yield from item
                else:
                    yield from FrameworkCallbackRepository._iter_source_anchors(item)
        elif isinstance(value, list):
            for item in value:
                yield from FrameworkCallbackRepository._iter_source_anchors(item)

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()

    @staticmethod
    def _finish_if_ready(conn, review, attempt_no: int) -> bool:
        attempt = conn.execute(
            """
            SELECT status FROM contract_framework_attempt
            WHERE review_id = %s AND attempt_no = %s
            """,
            (review["id"], attempt_no),
        ).fetchone()
        final_stage = conn.execute(
            """
            SELECT result_json FROM contract_review_stage_result
            WHERE review_id = %s AND attempt_no = %s
              AND callback_type = 'STAGE_RESULT' AND stage_id = 'finalize_review'
              AND validation_status = 'VALIDATED'
            ORDER BY event_sequence DESC, received_at DESC
            LIMIT 1
            """,
            (review["id"], attempt_no),
        ).fetchone()
        stage_rows = conn.execute(
            """
            SELECT DISTINCT stage_id FROM contract_review_stage_result
            WHERE review_id = %s AND attempt_no = %s
              AND callback_type = 'STAGE_RESULT' AND validation_status = 'VALIDATED'
            """,
            (review["id"], attempt_no),
        ).fetchall()
        completed_stages = {row["stage_id"] for row in stage_rows}
        if (
            attempt is None
            or attempt["status"] != "SUCCEEDED"
            or final_stage is None
            or completed_stages != REQUIRED_STAGE_IDS
        ):
            return False
        raw = dict(final_stage["result_json"])
        raw.pop("result_type", None)
        result_hash, _canonical = compute_result_hash(raw)
        raw["result_hash"] = result_hash
        validated = ReviewResultData.model_validate(raw).model_dump(mode="json")
        existing = conn.execute(
            "SELECT result_hash FROM contract_review_result WHERE review_id = %s FOR UPDATE",
            (review["id"],),
        ).fetchone()
        if existing is not None and existing["result_hash"] != result_hash:
            raise FrameworkCallbackRepository._mismatch(
                "Review result was already finalized with a different hash"
            )
        if existing is None:
            digest = hashlib.sha256(f"{review['id']}\0{result_hash}".encode("utf-8")).hexdigest()[:32]
            conn.execute(
                """
                INSERT INTO contract_review_result (
                  id, tenant_id, review_id, attempt_no, schema_version, result_hash, result_json
                ) VALUES (%s, %s, %s, %s, '1.0', %s, %s)
                """,
                (
                    f"result-{digest}",
                    review["tenant_id"],
                    review["id"],
                    attempt_no,
                    result_hash,
                    Jsonb(validated),
                ),
            )
        conn.execute(
            """
            UPDATE contract_review_run
            SET status = 'SUCCEEDED', current_stage = 'FINALIZING',
                error_code = NULL, error_message = NULL, retryable = false,
                user_action_required = false, error_details_json = NULL,
                completed_at = now(), version = version + 1
            WHERE id = %s AND status = 'RUNNING'
            """,
            (review["id"],),
        )
        return True

    @staticmethod
    def _mismatch(message: str) -> ContractError:
        return ContractError(
            "FRAMEWORK_CALLBACK_MISMATCH",
            message,
            status_code=409,
            retryable=False,
        )
