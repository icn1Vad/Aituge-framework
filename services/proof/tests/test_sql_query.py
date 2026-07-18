from __future__ import annotations

import pytest

from proof.application.sql_query import PolicySqlQueryService
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.postgres.repository import ProofRepository


class FakeExecutor:
    def __init__(self, rows=None) -> None:
        self.rows = rows or [{"policy_count": 85}]
        self.call = None

    def execute_read_query(self, sql, *, statement_timeout_ms, row_limit):
        self.call = (sql, statement_timeout_ms, row_limit)
        return {"columns": list(self.rows[0]), "rows": self.rows, "execution_ms": 2.5}


class FakeReadCursor:
    description = [type("Column", (), {"name": "policy_title"})()]

    def fetchall(self):
        return [{"policy_title": "采购管理办法"}]


class FakeReadConnection:
    def __init__(self) -> None:
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if "proof_user_query" in query:
            return FakeReadCursor()
        return self


def test_sql_query_service_normalizes_and_executes_read_query() -> None:
    executor = FakeExecutor()
    service = PolicySqlQueryService(Settings(_env_file=None), executor)
    result = service.execute(
        question="有多少制度？",
        sql=" SELECT count(*) AS policy_count FROM proof_sql_policy_v; ",
    )

    assert result["rows"] == [{"policy_count": 85}]
    assert result["truncated"] is False
    assert executor.call == (
        "SELECT count(*) AS policy_count FROM proof_sql_policy_v",
        5000,
        100,
    )


@pytest.mark.parametrize("sql", ["DELETE FROM proof_policy", "SELECT 1; SELECT 2", "  "])
def test_sql_query_service_rejects_non_single_read_queries(sql: str) -> None:
    service = PolicySqlQueryService(Settings(_env_file=None), FakeExecutor())
    with pytest.raises(ProofError):
        service.execute(question="test", sql=sql)


def test_sql_query_service_rejects_raw_proof_tables() -> None:
    service = PolicySqlQueryService(Settings(_env_file=None), FakeExecutor())
    with pytest.raises(ProofError) as exc_info:
        service.execute(question="查看草稿", sql="SELECT * FROM proof_policy")
    assert exc_info.value.code == "sql_relation_not_allowed"


def test_sql_query_service_marks_character_truncation() -> None:
    executor = FakeExecutor([{"text": "很长" * 1000}])
    service = PolicySqlQueryService(
        Settings(_env_file=None, sql_max_response_chars=1000),
        executor,
    )
    result = service.execute(question="全文", sql="SELECT text FROM proof_sql_clause_v")
    assert result["rows"] == []
    assert result["truncated"] is True


def test_postgres_read_query_preserves_percent_patterns() -> None:
    connection = FakeReadConnection()
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection

    result = repository.execute_read_query(
        "SELECT policy_title FROM proof_sql_policy_v WHERE policy_title ILIKE '%采购%'",
        statement_timeout_ms=5000,
        row_limit=5,
    )

    wrapped_query, wrapped_params = connection.calls[-1]
    assert "ILIKE '%采购%'" in wrapped_query
    assert wrapped_query.endswith("LIMIT 6")
    assert wrapped_params is None
    assert result["rows"] == [{"policy_title": "采购管理办法"}]
