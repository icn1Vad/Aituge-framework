from __future__ import annotations

from proof.infrastructure.postgres.repository import ProofRepository


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row if isinstance(self.row, list) else []


class _Connection:
    def __init__(self, rows: list[dict | None]):
        self.rows = iter(rows)
        self.executions: list[tuple[str, tuple | None]] = []
        self.committed = False

    def execute(self, sql: str, params: tuple | None = None):
        self.executions.append((sql, params))
        return _Result(next(self.rows))

    def commit(self) -> None:
        self.committed = True


class _ConnectionContext:
    def __init__(self, connection: _Connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


def test_resolved_draft_can_be_selected_as_next_version() -> None:
    connection = _Connection(
        [
            {
                "id": "policy-new",
                "family_id": "policy-new",
                "supersedes_policy_id": None,
                    "title": "待定名称",
                    "normalized_title": "待定名称",
                    "version": "v1.0.2",
                    "version_seq": 2,
                "similarity_state": "decision_required",
                "similarity_report": {
                    "candidates": [{"policy_id": "policy-draft"}]
                },
                "status": "draft",
            },
                {
                "id": "policy-draft",
                "family_id": "family-1",
                "title": "正式制度名称",
                "normalized_title": "正式制度名称",
                "version_seq": 1,
                "status": "draft",
                    "similarity_state": "new_version",
                },
                None,
                {"max_version": 1},
                None,
        ]
    )
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: _ConnectionContext(connection)
    repository.get_policy = lambda policy_id: {
        "id": policy_id,
        "title": "正式制度名称",
        "version": "v1.0.2",
        "version_seq": 2,
    }

    result = repository.decide_policy_similarity(
        "policy-new",
        decision="new_version",
        candidate_policy_id="policy-draft",
        idempotency_key="draft-next-version",
    )

    update_params = connection.executions[-1][1]
    assert connection.committed is True
    assert result["version"] == "v1.0.2"
    assert update_params is not None
    assert update_params[:6] == (
        "family-1",
        "policy-draft",
        "正式制度名称",
        "正式制度名称",
        2,
        "v1.0.2",
    )


def test_separate_policy_gets_next_available_title_suffix() -> None:
    connection = _Connection(
        [
                {
                "id": "policy-new",
                "family_id": "policy-new",
                "supersedes_policy_id": None,
                "title": "研发项目管理办法",
                "normalized_title": "研发项目管理",
                "version": "v1.0.0",
                "version_seq": 0,
                "similarity_state": "decision_required",
                "similarity_report": {"candidates": [{"policy_id": "policy-old"}]},
                    "status": "draft",
                },
                None,
                [
                {"title": "研发项目管理办法"},
                {"title": "研发项目管理办法-2"},
            ],
            None,
        ]
    )
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: _ConnectionContext(connection)
    repository.get_policy = lambda policy_id: {
        "id": policy_id,
        "title": "研发项目管理办法-3",
        "normalized_title": "研发项目管理3",
        "version": "v1.0.0",
        "version_seq": 0,
        "similarity_state": "separate",
    }

    result = repository.decide_policy_similarity(
        "policy-new",
        decision="separate",
        idempotency_key="separate-policy",
    )

    update_params = connection.executions[-1][1]
    assert connection.committed is True
    assert result["title"] == "研发项目管理办法-3"
    assert update_params is not None
    assert update_params[:2] == ("研发项目管理办法-3", "研发项目管理3")
