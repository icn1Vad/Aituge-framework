from __future__ import annotations

from fastapi.testclient import TestClient

from proof.api.app import create_app
from proof.config import Settings
from proof.errors import ProofError


class FakeService:
    def health(self):
        return {"ok": True, "embedding_configured": False}

    def list_levels(self):
        return [{"code": "upper"}, {"code": "peer"}, {"code": "lower"}]

    def list_categories(self):
        return [{"code": "governance"}, {"code": "finance"}, {"code": "general"}]

    def ingest_policy(self, **values):
        if values["content"] == b"invalid":
            raise ProofError(
                "no_clauses_found",
                "No formal clause was found.",
                status_code=422,
                details={"ingestion_run_id": "run-failed"},
            )
        return {
            "policy": {"id": "policy-1", "status": "draft"},
            "document": {"id": "document-1"},
            "clauses": [],
            "reused": False,
            "ingestion_run_id": "run-created",
        }

    def get_ingestion_run(self, run_id: str):
        if run_id != "run-1":
            raise ProofError("ingestion_run_not_found", "Ingestion run not found.", status_code=404)
        return {
            "id": run_id,
            "status": "succeeded",
            "stage": "complete",
            "reused": False,
            "block_count": 3,
            "clause_count": 2,
            "warning_count": 0,
        }

    def get_quality_report(self, policy_id: str):
        if policy_id != "policy-1":
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        return {
            "policy": {"id": policy_id},
            "clause_count": 4,
            "has_findings": True,
            "finding_counts": {
                "duplicate_number": 1,
                "missing_number": 1,
                "mixed_structure": 1,
                "semantic_ambiguity": 1,
            },
            "findings": [],
            "semantic_audit": {"status": "completed", "error_message": None},
        }

    def retry_semantic_audit(self, policy_id: str):
        return {"status": "running", "error_message": None}

    def confirm_policy(self, policy_id: str):
        return {"id": policy_id, "status": "effective"}

    def discard_policy(self, policy_id: str):
        return {"id": policy_id, "status": "discarded"}

    def accept_semantic_audit_result(self, payload):
        return {"audit_id": payload["audit_id"], "status": "completed", "finding_count": 0}

    def audit_dataset(self, *, refresh: bool = False):
        return {
            "root_name": "数据集",
            "summary": {
                "candidate_file_count": 1,
                "ready_file_count": 1,
                "blocked_file_count": 0,
                "anomaly_file_count": 0,
                "block_count": 2,
                "clause_count": 1,
                "ingested_file_count": 0,
            },
            "groups": [],
            "categories": [],
            "files": [{"id": "file-1"}],
        }

    def get_dataset_file(self, file_id: str):
        if file_id != "file-1":
            raise ProofError("dataset_file_not_found", "Dataset file not found.", status_code=404)
        return {"id": file_id, "clauses": [{"clause_no_raw": "第一条"}]}

    def index_document(self, document_id: str):
        raise ProofError("embedding_unconfigured", "Embedding API is not configured.", status_code=503)

    def search(self, **kwargs):
        raise ProofError("embedding_unconfigured", "Embedding API is not configured.", status_code=503)

    def execute_sql(self, **kwargs):
        return {
            "question": kwargs["question"],
            "sql": kwargs["sql"],
            "columns": ["policy_count"],
            "rows": [{"policy_count": 85}],
            "row_count": 1,
            "truncated": False,
            "execution_ms": 1.0,
        }


def test_metadata_endpoints() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    assert client.get("/health").status_code == 200
    assert [item["code"] for item in client.get("/v1/meta/policy-levels").json()["data"]] == [
        "upper",
        "peer",
        "lower",
    ]
    assert len(client.get("/v1/categories").json()["data"]) == 3


def test_embedding_endpoints_are_explicitly_unavailable() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    index_response = client.post("/v1/documents/document-1/index")
    assert index_response.status_code == 503
    assert index_response.json()["error"] == "embedding_unconfigured"

    search_response = client.post("/v1/retrieval/search", json={"query": "审批权限"})
    assert search_response.status_code == 503
    assert search_response.json()["error"] == "embedding_unconfigured"


def test_get_ingestion_run_and_unknown_id() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    response = client.get("/v1/ingestion-runs/run-1")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "id": "run-1",
        "status": "succeeded",
        "stage": "complete",
        "reused": False,
        "block_count": 3,
        "clause_count": 2,
        "warning_count": 0,
    }

    missing = client.get("/v1/ingestion-runs/missing")
    assert missing.status_code == 404
    assert missing.json()["error"] == "ingestion_run_not_found"


def test_policy_upload_exposes_run_id_on_success_and_failure() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    created = client.post("/v1/policies", files={"file": ("policy.txt", b"valid", "text/plain")})
    assert created.status_code == 200
    assert created.json()["data"]["ingestion_run_id"] == "run-created"

    failed = client.post("/v1/policies", files={"file": ("policy.txt", b"invalid", "text/plain")})
    assert failed.status_code == 422
    assert failed.json()["details"]["ingestion_run_id"] == "run-failed"


def test_quality_report_endpoint() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    response = client.get("/v1/policies/policy-1/quality-report")
    assert response.status_code == 200
    assert response.json()["data"]["finding_counts"] == {
        "duplicate_number": 1,
        "missing_number": 1,
        "mixed_structure": 1,
        "semantic_ambiguity": 1,
    }
    assert client.get("/v1/policies/missing/quality-report").status_code == 404


def test_dataset_page_and_audit_endpoints() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    page = client.get("/dataset")
    assert page.status_code == 200
    assert "Proof 数据集入库检查台" in page.text

    audit = client.get("/v1/dataset/audit?refresh=true")
    assert audit.status_code == 200
    assert audit.json()["data"]["summary"]["candidate_file_count"] == 1

    detail = client.get("/v1/dataset/files/file-1")
    assert detail.status_code == 200
    assert detail.json()["data"]["clauses"][0]["clause_no_raw"] == "第一条"

    missing = client.get("/v1/dataset/files/missing")
    assert missing.status_code == 404
    assert missing.json()["error"] == "dataset_file_not_found"


def test_workbench_and_experiment_policy_are_available() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    home = client.get("/")
    assert home.status_code == 200
    assert "Proof 制度工作台" in home.text
    assert "重复编号、编号缺失、混合结构" in home.text

    workbench = client.get("/workbench")
    assert workbench.status_code == 200
    assert "上传并开始审校" in workbench.text
    assert "确认入库" in workbench.text

    experiment = client.get("/examples/policy-structure-errors.txt")
    assert experiment.status_code == 200
    assert "第二条 本条故意与上一条使用相同编号" in experiment.text
    assert "第四条 本条故意跳过第三条" in experiment.text


def test_sql_query_endpoint() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    response = client.post(
        "/v1/query/sql",
        json={"question": "有多少份制度？", "sql": "SELECT count(*) AS policy_count FROM proof_sql_policy_v"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["rows"] == [{"policy_count": 85}]


def test_review_workflow_endpoints() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    retry = client.post("/v1/policies/policy-1/semantic-audit")
    assert retry.json()["data"]["status"] == "running"
    confirmed = client.post("/v1/policies/policy-1/confirm")
    assert confirmed.json()["data"]["status"] == "effective"
    discarded = client.delete("/v1/policies/policy-1")
    assert discarded.json()["data"]["status"] == "discarded"
    callback = client.post(
        "/v1/internal/semantic-audits/result",
        json={"audit_id": "audit-1", "output": {"summary": {}, "items": []}},
    )
    assert callback.json()["data"]["status"] == "completed"
