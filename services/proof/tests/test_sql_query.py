from __future__ import annotations

import pytest

from proof.application.sql_query import PolicySqlQueryService
from proof.config import Settings
from proof.errors import ProofError


class FakeExecutor:
    def __init__(self, rows=None) -> None:
        self.rows = rows or [{"policy_count": 85}]
        self.call = None

    def execute_read_query(self, sql, *, statement_timeout_ms, row_limit):
        self.call = (sql, statement_timeout_ms, row_limit)
        return {"columns": list(self.rows[0]), "rows": self.rows, "execution_ms": 2.5}


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
