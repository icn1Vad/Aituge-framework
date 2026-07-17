from __future__ import annotations

import json
import re
from typing import Any, Protocol

from proof.config import Settings
from proof.errors import ProofError


READ_QUERY_START = re.compile(r"^(?:select|with)\b", re.IGNORECASE)
PROOF_RELATION = re.compile(r"\b(proof_[a-z0-9_]+)\b", re.IGNORECASE)
ALLOWED_PROOF_RELATIONS = {"proof_sql_policy_v", "proof_sql_clause_v"}


class ReadQueryExecutor(Protocol):
    def execute_read_query(
        self,
        sql: str,
        *,
        statement_timeout_ms: int,
        row_limit: int,
    ) -> dict[str, Any]: ...


class PolicySqlQueryService:
    """Validate the execution envelope; the Agent remains responsible for SQL generation."""

    def __init__(self, settings: Settings, executor: ReadQueryExecutor) -> None:
        self.executor = executor
        self.timeout_ms = int(settings.sql_statement_timeout_seconds * 1000)
        self.row_limit = settings.sql_max_rows
        self.response_char_limit = settings.sql_max_response_chars

    def execute(self, *, question: str, sql: str) -> dict[str, Any]:
        normalized = _normalize_read_query(sql)
        result = self.executor.execute_read_query(
            normalized,
            statement_timeout_ms=self.timeout_ms,
            row_limit=self.row_limit,
        )
        rows, char_truncated = _limit_serialized_rows(
            result["rows"][: self.row_limit],
            self.response_char_limit,
        )
        row_truncated = len(result["rows"]) > self.row_limit
        return {
            "question": question.strip(),
            "sql": normalized,
            "columns": result["columns"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": row_truncated or char_truncated,
            "execution_ms": result["execution_ms"],
        }


def _normalize_read_query(sql: str) -> str:
    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        raise ProofError("empty_sql", "SQL query is empty.", status_code=422)
    if ";" in normalized:
        raise ProofError(
            "sql_multiple_statements",
            "Only one SQL query can be executed at a time.",
            status_code=422,
        )
    if not READ_QUERY_START.match(normalized):
        raise ProofError(
            "sql_read_only_required",
            "SQL query must start with SELECT or WITH.",
            status_code=422,
        )
    relations = {match.casefold() for match in PROOF_RELATION.findall(normalized)}
    forbidden = sorted(relations - ALLOWED_PROOF_RELATIONS)
    if forbidden:
        raise ProofError(
            "sql_relation_not_allowed",
            "SQL queries may only read the published Proof semantic views.",
            status_code=422,
            details={"forbidden_relations": forbidden},
        )
    return normalized


def _limit_serialized_rows(
    rows: list[dict[str, Any]],
    char_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    accepted: list[dict[str, Any]] = []
    used = 2
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False, default=str)) + (1 if accepted else 0)
        if used + size > char_limit:
            return accepted, True
        accepted.append(row)
        used += size
    return accepted, False
