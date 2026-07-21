from __future__ import annotations

from proof.infrastructure.postgres.repository import ProofRepository


class _Rows:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, query: str, params: tuple[str]):
        self.queries.append(query)
        if "proof_conflict_audit_finding" in query:
            return _Rows(
                [
                    {
                        "id": "source-unit",
                        "candidate_ids": ["candidate-unit"],
                        "conflict_type": "numeric_conflict",
                        "problem": "期限不一致。",
                        "suggestion": "统一期限。",
                    }
                ]
            )
        return _Rows(
            [
                {
                    "id": "source-unit",
                    "category": "semantic_ambiguity",
                    "problem": "主体不明确。",
                    "suggestion": "明确责任主体。",
                }
            ]
        )


class _AuditRunConnection:
    def __init__(self, run: dict) -> None:
        self.run = run
        self.queries: list[str] = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, query: str, params: tuple[str]):
        self.queries.append(query)
        if query.lstrip().startswith("SELECT status"):
            return _Rows([self.run])
        return _Rows([])

    def commit(self) -> None:
        self.committed = True


def test_finding_queries_return_only_chunk_ids_without_duplicate_location_fields() -> None:
    connection = _Connection()
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection

    semantic = repository.list_audit_findings("audit-1")
    conflicts = repository.list_conflict_audit_findings("audit-1")

    assert semantic == [
        {
            "id": "source-unit",
            "category": "semantic_ambiguity",
            "problem": "主体不明确。",
            "suggestion": "明确责任主体。",
        }
    ]
    assert conflicts == [
        {
            "id": "source-unit",
            "candidate_ids": ["candidate-unit"],
            "conflict_type": "numeric_conflict",
            "problem": "期限不一致。",
            "suggestion": "统一期限。",
        }
    ]
    selected_columns = [query.split("FROM", maxsplit=1)[0] for query in connection.queries]
    assert all("u.clause_ordinal" not in columns for columns in selected_columns)
    assert all("u.clause_no_raw" not in columns for columns in selected_columns)


def test_partial_backfill_preserves_completed_semantic_findings() -> None:
    connection = _AuditRunConnection(
        {"status": "completed", "summary_status": "pending", "conflict_status": "pending"}
    )
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection

    assert repository.prepare_audit_run_for_dispatch("audit-1") is True

    statements = "\n".join(connection.queries)
    assert "DELETE FROM proof_audit_finding" not in statements
    assert "DELETE FROM proof_conflict_audit_finding" in statements
    assert "summary_status = 'completed'" in statements
    assert "conflict_status = 'completed'" in statements
    assert connection.committed is True
