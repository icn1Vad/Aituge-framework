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
        if "conflict_audit_finding" in query:
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
    intra_conflicts = repository.list_intra_conflict_audit_findings("audit-1")

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
    assert intra_conflicts == conflicts
    selected_columns = [query.split("FROM", maxsplit=1)[0] for query in connection.queries]
    assert all("u.clause_ordinal" not in columns for columns in selected_columns)
    assert all("u.clause_no_raw" not in columns for columns in selected_columns)


def test_partial_backfill_preserves_completed_semantic_findings() -> None:
    connection = _AuditRunConnection(
        {
            "status": "completed",
            "summary_status": "pending",
            "conflict_status": "pending",
            "intra_conflict_status": "pending",
        }
    )
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection

    assert repository.prepare_audit_run_for_dispatch("audit-1") is True

    statements = "\n".join(connection.queries)
    assert "DELETE FROM proof_audit_finding" not in statements
    assert "DELETE FROM proof_conflict_audit_finding" in statements
    assert "DELETE FROM proof_intra_conflict_audit_finding" in statements
    assert "summary_status = 'completed'" in statements
    assert "conflict_status = 'completed'" in statements
    assert connection.committed is True


class _ConfirmConnection:
    def __init__(self) -> None:
        self.queries = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def execute(self, query, params):
        self.queries.append(query)
        if query.lstrip().startswith("SELECT id, family_id"):
            return _Rows([{
                "id": "policy-1",
                "family_id": "family-1",
                "normalized_title": "policy",
            }])
        if query.lstrip().startswith("SELECT status"):
            return _Rows([{
                "status": "draft",
                "id": "policy-1",
                "family_id": "family-1",
                "normalized_title": "policy",
                "supersedes_policy_id": None,
                "version_seq": 0,
                "similarity_state": "clear",
            }])
        return _Rows([])

    def commit(self):
        self.committed = True


class _ReplacementConnection(_ConfirmConnection):
    def __init__(self) -> None:
        super().__init__()
        self.parameters = []

    def execute(self, query, params):
        self.queries.append(query)
        self.parameters.append(params)
        if query.lstrip().startswith("SELECT id, family_id"):
            return _Rows([{
                "id": "policy-old",
                "family_id": "family-1",
                "normalized_title": "policy",
            }])
        if query.lstrip().startswith("SELECT status"):
            return _Rows([{
                "status": "expired",
                "id": "policy-old",
                "family_id": "family-1",
                "normalized_title": "policy",
                "supersedes_policy_id": None,
                "version_seq": 0,
                "similarity_state": "clear",
            }])
        if query.lstrip().startswith("SELECT id, title"):
            return _Rows([{
                "id": "policy-new",
                "title": "Policy",
                "version": "v1.0.2",
                "version_seq": 2,
            }])
        return _Rows([])


class _HigherVersionConnection(_ConfirmConnection):
    def __init__(self) -> None:
        super().__init__()
        self.parameters = []

    def execute(self, query, params):
        self.queries.append(query)
        self.parameters.append(params)
        if query.lstrip().startswith("SELECT id, family_id"):
            return _Rows([{
                "id": "policy-high",
                "family_id": "family-1",
                "normalized_title": "policy",
            }])
        if query.lstrip().startswith("SELECT status"):
            return _Rows([{
                "status": "draft",
                "id": "policy-high",
                "family_id": "family-1",
                "normalized_title": "policy",
                "supersedes_policy_id": "policy-middle",
                "version_seq": 2,
                "similarity_state": "new_version",
            }])
        if query.lstrip().startswith("SELECT id, title"):
            return _Rows([{
                "id": "policy-low",
                "title": "Policy",
                "version": "v1.0.0",
                "version_seq": 0,
            }])
        return _Rows([])


def test_confirm_deletes_temporary_embeddings_without_formal_index_reuse():
    connection = _ConfirmConnection()
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection
    repository.get_policy = lambda policy_id: {"id": policy_id, "status": "effective"}

    assert repository.confirm_policy("policy-1")["status"] == "effective"
    statements = "\n".join(connection.queries)
    assert "DELETE FROM proof_draft_retrieval_embedding" in statements
    assert "proof_retrieval_embedding" not in statements


def test_replacing_higher_version_expires_and_retires_only_other_versions():
    connection = _ReplacementConnection()
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection
    repository.get_policy = lambda policy_id: {"id": policy_id, "status": "effective"}

    activated = repository.confirm_policy("policy-old", replace_existing=True)

    assert activated["status"] == "effective"
    statements = "\n".join(connection.queries)
    assert "AND id <> %s" in statements
    assert statements.count("AND p.id <> %s") == 2
    assert ("policy-old", "family-1", "policy") in connection.parameters
    assert connection.committed is True


def test_higher_version_automatically_replaces_indirect_lower_effective_version():
    connection = _HigherVersionConnection()
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection
    repository.get_policy = lambda policy_id: {"id": policy_id, "status": "effective"}

    activated = repository.confirm_policy("policy-high")

    assert activated["status"] == "effective"
    assert ("policy-high", "family-1", "policy") in connection.parameters
    assert connection.committed is True
