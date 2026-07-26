from __future__ import annotations

import copy
import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from contract.api.models import ReviewResultData
from contract.application.idempotency import canonical_json, normalize_party_name
from contract.application.result_hash import compute_result_hash
from contract.callback.models import FrameworkCallback, StageExecuteRequest
from contract.config import Settings
from contract.errors import ConfigurationError, ContractError
from contract.ir.models import ContractIR


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
LEGACY_REQUIRED_STAGE_IDS = frozenset(STAGE_TO_REVIEW_STAGE)
DIRECT_REQUIRED_STAGE_IDS = frozenset(
    {
        "parse_contract",
        "resolve_parties",
        "extract_contract_ir",
        "finalize_review",
    }
)
REQUIRED_STAGE_PROFILES = frozenset(
    {
        LEGACY_REQUIRED_STAGE_IDS,
        DIRECT_REQUIRED_STAGE_IDS,
    }
)


def _matches_required_stage_profile(completed_stages: set[str]) -> bool:
    return frozenset(completed_stages) in REQUIRED_STAGE_PROFILES


MODEL_REVIEW_STAGE_IDS = frozenset(
    {
        "rights_obligations_review",
        "commercial_terms_review",
        "liability_termination_review",
        "missing_ambiguous_clauses",
        "relation_extraction",
    }
)
SEMANTIC_IR_FIELDS = (
    "definitions",
    "rights",
    "obligations",
    "prohibitions",
    "payment_terms",
    "delivery_terms",
    "acceptance_terms",
    "liabilities",
    "termination_terms",
    "confidentiality_terms",
    "intellectual_property_terms",
    "dispute_resolution",
    "dates",
    "amounts",
)


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

    def get_revision_source_snapshot(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Read the completed result and the parse Generation that produced it.

        The result Attempt, rather than the document's currently active
        Generation, is authoritative.  Ownership predicates intentionally make
        a foreign-tenant or foreign-user review indistinguishable from a
        missing review.
        """

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT review.id AS review_id,
                       review.status AS review_status,
                       result.result_hash,
                       result.result_json,
                       generation.id AS generation_id,
                       generation.status AS generation_status,
                       generation.contract_ir_json
                FROM contract_review_run review
                LEFT JOIN contract_review_result result
                  ON result.review_id = review.id
                 AND result.tenant_id = review.tenant_id
                LEFT JOIN LATERAL (
                    SELECT parse_generation.*
                    FROM contract_review_stage_result stage
                    JOIN contract_parse_generation parse_generation
                      ON parse_generation.id = stage.result_json ->> 'generation_id'
                     AND parse_generation.document_id = review.document_id
                     AND parse_generation.tenant_id = review.tenant_id
                    WHERE stage.review_id = review.id
                      AND stage.attempt_no = result.attempt_no
                      AND stage.callback_type = 'STAGE_RESULT'
                      AND stage.stage_id = 'parse_contract'
                      AND stage.validation_status = 'VALIDATED'
                    ORDER BY stage.event_sequence DESC, stage.received_at DESC
                    LIMIT 1
                ) generation ON true
                WHERE review.id = %s
                  AND review.tenant_id = %s
                  AND review.user_id = %s
                LIMIT 1
                """,
                (review_id, tenant_id, user_id),
            ).fetchone()
        if row is None:
            raise ContractError(
                "REVIEW_NOT_FOUND",
                "Contract review does not exist or is not accessible",
                status_code=404,
            )
        return dict(row)

    def finish_if_ready(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
        attempt_no: int,
    ) -> bool:
        """Idempotently finalize a terminal Framework attempt from persisted callbacks."""

        with self.connect() as conn:
            review = conn.execute(
                """
                SELECT * FROM contract_review_run
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                FOR UPDATE
                """,
                (review_id, tenant_id, user_id),
            ).fetchone()
            if review is None:
                raise ContractError(
                    "REVIEW_NOT_FOUND",
                    "Contract review does not exist or is not accessible",
                    status_code=404,
                )
            if review["status"] == "SUCCEEDED":
                conn.commit()
                return True
            if (
                review["status"] != "RUNNING"
                or review["active_attempt_no"] != attempt_no
            ):
                conn.commit()
                return False
            finished = self._finish_if_ready(conn, review, attempt_no)
            conn.commit()
            return finished

    def get_stage_execution_context(self, request: StageExecuteRequest) -> dict[str, Any]:
        """Return the active execution context, claiming a matching CREATING Attempt if needed.

        Framework starts a Run before its create response reaches Contract Python.  The
        first gateway stage can therefore arrive while the new Attempt is still CREATING.
        Binding the exact Task/Run mapping here closes that race without performing any
        Framework HTTP request while the review row is locked.
        """
        with self.connect() as conn:
            review = conn.execute(
                "SELECT * FROM contract_review_run WHERE id = %s FOR UPDATE",
                (request.review_id,),
            ).fetchone()
            if review is None:
                raise ContractError("REVIEW_NOT_FOUND", "Contract review does not exist", status_code=404)
            attempt = conn.execute(
                """
                SELECT * FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (request.review_id, request.attempt_no),
            ).fetchone()
            if attempt is None:
                raise self._mismatch("Framework Attempt does not exist")
            if not self._stage_task_matches_review(review, request):
                raise self._mismatch("Framework task input does not match the contract review")

            mapping_matches = (
                attempt["framework_task_id"] == request.framework_task_id
                and attempt["framework_run_id"] == request.framework_run_id
            )
            already_active = (
                review["status"] == "RUNNING"
                and review["active_attempt_no"] == request.attempt_no
                and attempt["status"] in {"RUNNING", "SUCCEEDED"}
                and bool(attempt["is_active"])
                and mapping_matches
            )
            if not already_active:
                mapping_available = (
                    attempt["framework_task_id"] is None
                    and attempt["framework_run_id"] is None
                ) or mapping_matches
                can_claim = (
                    review["status"] in {"CREATED", "RUNNING"}
                    and not review["cancel_requested"]
                    and attempt["status"] == "CREATING"
                    and not attempt["is_active"]
                    and mapping_available
                    and (review["active_attempt_no"] or 0) < request.attempt_no
                )
                if not can_claim:
                    raise self._mismatch(
                        "Framework stage execution does not match the active Attempt"
                    )
                if review["active_attempt_no"] is not None:
                    previous = conn.execute(
                        """
                        SELECT status FROM contract_framework_attempt
                        WHERE review_id = %s AND attempt_no = %s
                        FOR UPDATE
                        """,
                        (request.review_id, review["active_attempt_no"]),
                    ).fetchone()
                    if previous is None or previous["status"] != "ORPHANED":
                        raise self._mismatch("Previous Framework Attempt is not orphaned")

                stage = STAGE_TO_REVIEW_STAGE[request.stage_id]
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET is_active = false
                    WHERE review_id = %s AND is_active
                    """,
                    (request.review_id,),
                )
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET framework_task_id = %s, framework_run_id = %s,
                        status = 'RUNNING', current_stage = %s,
                        last_activity_at = now(), started_at = COALESCE(started_at, now()),
                        is_active = true
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (
                        request.framework_task_id,
                        request.framework_run_id,
                        stage,
                        request.review_id,
                        request.attempt_no,
                    ),
                )
                conn.execute(
                    """
                    UPDATE contract_review_run
                    SET status = 'RUNNING', current_stage = %s,
                        active_attempt_no = %s, started_at = COALESCE(started_at, now()),
                        version = version + 1
                    WHERE id = %s
                    """,
                    (stage, request.attempt_no, request.review_id),
                )
            conn.commit()
        return self.get_review_context(request.review_id)

    @staticmethod
    def _stage_task_matches_review(review: dict[str, Any], request: StageExecuteRequest) -> bool:
        task = request.task_input
        return (
            task.schema_version == review["schema_version"]
            and task.business_task_id == review["business_task_id"]
            and task.contract_version_id == review["contract_version_id"]
            and task.document_id == review["document_id"]
            and task.perspective == review["perspective"]
            and normalize_party_name(task.our_party_name) == review["our_party_name"]
            and task.contract_type == review["contract_type"]
            and task.review_attitude == review["review_attitude"]
        )

    def get_attempt_parse_generation(
        self,
        review_id: str,
        attempt_no: int,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            generation = conn.execute(
                """
                SELECT generation.*
                FROM contract_review_stage_result stage
                JOIN contract_review_run review ON review.id = stage.review_id
                JOIN contract_parse_generation generation
                  ON generation.id = stage.result_json ->> 'generation_id'
                 AND generation.document_id = review.document_id
                 AND generation.tenant_id = review.tenant_id
                WHERE stage.review_id = %s AND stage.attempt_no = %s
                  AND stage.callback_type = 'STAGE_RESULT'
                  AND stage.stage_id = 'parse_contract'
                  AND stage.validation_status = 'VALIDATED'
                  AND generation.status = 'SUCCEEDED'
                ORDER BY stage.event_sequence DESC, stage.received_at DESC
                LIMIT 1
                """,
                (review_id, attempt_no),
            ).fetchone()
        return dict(generation) if generation else None

    def get_validated_stage_result(
        self,
        review_id: str,
        attempt_no: int,
        stage_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT result_json
                FROM contract_review_stage_result
                WHERE review_id = %s AND attempt_no = %s
                  AND callback_type = 'STAGE_RESULT' AND stage_id = %s
                  AND validation_status = 'VALIDATED'
                ORDER BY event_sequence DESC, received_at DESC
                LIMIT 1
                """,
                (review_id, attempt_no, stage_id),
            ).fetchone()
        return dict(row["result_json"]) if row else None

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
                    self._promote_contract_ir(conn, review, payload["result"]["semantic_ir"])
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
                raise FrameworkCallbackRepository._party_unresolved(
                    review,
                    result,
                    "Resolved party perspective does not match the review",
                )
            FrameworkCallbackRepository._validate_party_sources(conn, review, result)
            return
        if stage_id in MODEL_REVIEW_STAGE_IDS:
            FrameworkCallbackRepository._validate_review_candidate_sources(
                conn,
                review,
                result,
            )
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

    @staticmethod
    def _promote_contract_ir(conn, review, semantic_ir: dict[str, Any]) -> None:
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
        generation = conn.execute(
            """
            SELECT generation.*
            FROM contract_review_stage_result stage
            JOIN contract_parse_generation generation
              ON generation.id = stage.result_json ->> 'generation_id'
            WHERE stage.review_id = %s AND stage.attempt_no = %s
              AND stage.callback_type = 'STAGE_RESULT' AND stage.stage_id = 'parse_contract'
              AND stage.validation_status = 'VALIDATED'
              AND generation.document_id = %s AND generation.tenant_id = %s
              AND generation.status IN ('RUNNING', 'SUCCEEDED')
            ORDER BY stage.event_sequence DESC, stage.received_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (
                review["id"],
                review["active_attempt_no"],
                review["document_id"],
                review["tenant_id"],
            ),
        ).fetchone()
        if generation is None:
            raise FrameworkCallbackRepository._mismatch("Contract IR generation does not exist")
        structural_ir = generation["contract_ir_json"]
        if not isinstance(structural_ir, dict):
            raise ContractError("RESULT_INVALID", "Structural Contract IR is unavailable", status_code=422)
        blocks = conn.execute(
            """
            SELECT block_id, block_no, page_number, text
            FROM contract_document_block
            WHERE generation_id = %s AND tenant_id = %s
            ORDER BY block_no
            """,
            (generation["id"], review["tenant_id"]),
        ).fetchall()
        document = structural_ir.get("document", {})
        if len(blocks) <= 0 or len(blocks) != document.get("block_count"):
            raise ContractError("RESULT_INVALID", "Contract IR block count is invalid", status_code=422)

        parties = [
            FrameworkCallbackRepository._materialize_party(
                "PARTY_A", party["party_a"]["name"], blocks, generation["id"]
            ),
            FrameworkCallbackRepository._materialize_party(
                "PARTY_B", party["party_b"]["name"], blocks, generation["id"]
            ),
        ]
        contract_ir = copy.deepcopy(structural_ir)
        contract_ir["parties"] = parties
        contract_ir["our_party"] = None
        contract_ir["counterparty"] = None
        contract_ir["contract_type"] = party["contract_type"]
        for field in SEMANTIC_IR_FIELDS:
            contract_ir[field] = semantic_ir[field]
        contract_ir = ContractIR.model_validate(contract_ir).model_dump(mode="json")
        FrameworkCallbackRepository._validate_ir_sources(contract_ir, list(blocks))

        if generation["status"] == "SUCCEEDED":
            persisted = ContractIR.model_validate(structural_ir).model_dump(mode="json")
            persisted_names = {item["role"]: item["name"] for item in persisted["parties"]}
            expected_names = {item["role"]: item["name"] for item in parties}
            if (
                persisted_names != expected_names
                or persisted["contract_type"] != party["contract_type"]
            ):
                raise FrameworkCallbackRepository._mismatch(
                    "Completed Contract IR does not match the resolved contract identity"
                )
            FrameworkCallbackRepository._validate_ir_sources(persisted, list(blocks))
            return

        ir_hash = "sha256:" + hashlib.sha256(canonical_json(contract_ir).encode("utf-8")).hexdigest()
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
            ORDER BY block_no
            """,
            (generation["generation_id"], review["tenant_id"]),
        ).fetchall()
        party_a = result["party_a"]["name"]
        party_b = result["party_b"]["name"]
        if FrameworkCallbackRepository._normalized_text(party_a) == FrameworkCallbackRepository._normalized_text(
            party_b
        ):
            raise FrameworkCallbackRepository._party_unresolved(
                review,
                result,
                "Resolved contract parties must be distinct",
            )
        missing = [
            name
            for name in (party_a, party_b)
            if not any(name in row["text"] for row in rows)
        ]
        if missing:
            raise FrameworkCallbackRepository._party_unresolved(
                review,
                result,
                "Resolved party names must come from the current contract",
                extra_details={"missing_party_names": missing},
            )
        requested_our_party = normalize_party_name(review["our_party_name"])
        resolved_our_party = party_a if review["perspective"] == "PARTY_A" else party_b
        if requested_our_party is not None and FrameworkCallbackRepository._normalized_text(
            requested_our_party
        ) != FrameworkCallbackRepository._normalized_text(resolved_our_party):
            raise FrameworkCallbackRepository._party_unresolved(
                review,
                result,
                "Resolved party does not match the user-provided party name",
                extra_details={"requested_our_party_name": requested_our_party},
            )

    @staticmethod
    def _validate_review_candidate_sources(conn, review, result: dict[str, Any]) -> None:
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
            raise ContractError(
                "EVIDENCE_INVALID",
                "Review evidence requires a validated parse Generation",
                status_code=422,
            )
        rows = conn.execute(
            """
            SELECT block_id, page_number, text
            FROM contract_document_block
            WHERE generation_id = %s AND tenant_id = %s
            """,
            (generation["generation_id"], review["tenant_id"]),
        ).fetchall()
        blocks = {row["block_id"]: row for row in rows}
        findings = result["findings"]
        evidences = result["evidences"]
        finding_ids = {item["finding_id"] for item in findings}
        evidence_ids = {item["evidence_id"] for item in evidences}
        evidences_by_id = {item["evidence_id"]: item for item in evidences}
        evidence_ids_by_finding: dict[str, set[str]] = {}
        if len(finding_ids) != len(findings) or len(evidence_ids) != len(evidences):
            raise ContractError(
                "EVIDENCE_INVALID",
                "Review Stage Finding and Evidence IDs must be unique",
                status_code=422,
            )
        for finding in findings:
            if finding["perspective"] != review["perspective"]:
                raise ContractError(
                    "EVIDENCE_INVALID",
                    "Review Stage Finding perspective does not match the review",
                    status_code=422,
                )
            finding_evidence_ids = finding["evidence_ids"]
            if (
                not finding_evidence_ids
                or len(set(finding_evidence_ids)) != len(finding_evidence_ids)
                or any(evidence_id not in evidence_ids for evidence_id in finding_evidence_ids)
            ):
                raise ContractError(
                    "EVIDENCE_INVALID",
                    "Review Stage Finding evidence links are incomplete",
                    status_code=422,
                )
            evidence_ids_by_finding[finding["finding_id"]] = set(finding_evidence_ids)
            for evidence_id in finding_evidence_ids:
                evidence = evidences_by_id[evidence_id]
                if evidence["finding_id"] != finding["finding_id"]:
                    raise FrameworkCallbackRepository._invalid_evidence_candidate(
                        evidence_id,
                        "Finding and Evidence links are inconsistent",
                        finding_id=finding["finding_id"],
                        evidence_finding_id=evidence["finding_id"],
                    )
        for evidence in evidences:
            evidence_id = evidence["evidence_id"]
            if evidence["finding_id"] not in finding_ids:
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence references an unknown Finding",
                )
            if evidence_id not in evidence_ids_by_finding[evidence["finding_id"]]:
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence is not linked by its Finding",
                    evidence_finding_id=evidence["finding_id"],
                )
            if evidence["evidence_type"] == "ABSENCE":
                continue
            block = blocks.get(evidence["block_id"])
            if block is None:
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence block is outside this contract Generation",
                )
            start = evidence["char_start"]
            end = evidence["char_end"]
            if start is None or end is None or start >= end or end > len(block["text"]):
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence character range is outside its Block",
                    block_id=evidence["block_id"],
                    block_length=len(block["text"]),
                )
            quoted_text = block["text"][start:end]
            expected_hash = "sha256:" + hashlib.sha256(quoted_text.encode("utf-8")).hexdigest()
            if evidence["quoted_text"] is not None and evidence["quoted_text"] != quoted_text:
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence quoted text does not match its Block",
                    block_id=evidence["block_id"],
                )
            if (
                evidence["quoted_text_hash"] is not None
                and evidence["quoted_text_hash"] != expected_hash
            ):
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence text hash does not match its Block",
                    block_id=evidence["block_id"],
                )
            if evidence["page_number"] is not None and evidence["page_number"] != block["page_number"]:
                raise FrameworkCallbackRepository._invalid_evidence_candidate(
                    evidence_id,
                    "Evidence page does not match its Block",
                    block_id=evidence["block_id"],
                )

    @staticmethod
    def _materialize_party(
        role: str,
        name: str,
        rows: list[dict[str, Any]],
        generation_id: str,
    ) -> dict[str, Any]:
        for row in rows:
            start = row["text"].find(name)
            if start < 0:
                continue
            end = start + len(name)
            digest = hashlib.sha256(
                f"{generation_id}\0{role}\0{row['block_id']}\0{start}\0{end}".encode("utf-8")
            ).hexdigest()[:32]
            return {
                "role": role,
                "name": name,
                "source_anchors": [
                    {
                        "anchor_id": f"anchor-{digest}",
                        "block_id": row["block_id"],
                        "page_number": row["page_number"],
                        "char_start": start,
                        "char_end": end,
                    }
                ],
            }
        raise ContractError(
            "RESULT_INVALID",
            f"Resolved {role} name must exactly match text in a contract block",
            status_code=422,
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
                    f"Contract IR {party['role']} name is not supported by its source anchors",
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
            or not _matches_required_stage_profile(completed_stages)
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

    @staticmethod
    def _party_unresolved(
        review: dict[str, Any],
        result: dict[str, Any],
        message: str,
        *,
        extra_details: dict[str, Any] | None = None,
    ) -> ContractError:
        details = {
            "perspective": review["perspective"],
            "candidate_parties": [result["party_a"]["name"], result["party_b"]["name"]],
        }
        if extra_details:
            details.update(extra_details)
        return ContractError(
            "PARTY_UNRESOLVED",
            message,
            status_code=422,
            retryable=False,
            user_action_required=True,
            details=details,
        )

    @staticmethod
    def _invalid_evidence_candidate(
        evidence_id: str,
        message: str,
        *,
        block_id: str | None = None,
        block_length: int | None = None,
        finding_id: str | None = None,
        evidence_finding_id: str | None = None,
    ) -> ContractError:
        details: dict[str, Any] = {"evidence_id": evidence_id}
        if block_id is not None:
            details["block_id"] = block_id
        if block_length is not None:
            details["block_length"] = block_length
        if finding_id is not None:
            details["finding_id"] = finding_id
        if evidence_finding_id is not None:
            details["evidence_finding_id"] = evidence_finding_id
        return ContractError(
            "EVIDENCE_INVALID",
            message,
            status_code=422,
            retryable=False,
            details=details,
        )
