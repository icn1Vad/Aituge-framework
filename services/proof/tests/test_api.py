from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from proof.api.app import _conflict_agent_view, create_app
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
            "policy": self._policy(),
            "document": {"id": "document-1"},
            "clauses": [],
            "reused": False,
            "ingestion_run_id": "run-created",
        }

    @staticmethod
    def _policy():
        return {
            "id": "policy-1",
            "status": "draft",
            "level_code": "lower",
            "level_name": "三级制度",
            "category_code": "finance",
            "category_name": "财务管理",
            "level": {"code": "lower", "name": "三级制度", "sort_rank": 100},
            "category": {
                "code": "finance",
                "name": "财务管理",
                "description": "",
                "level": 1,
                "parent": None,
                "path_name": "财务管理",
            },
        }

    def list_policies(self, **filters):
        return [self._policy()]

    def get_policy(self, policy_id: str):
        if policy_id != "policy-1":
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        return self._policy()

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

    def get_audit_status(self, policy_id: str):
        if policy_id != "policy-1":
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        return {
            "id": "audit-1",
            "policy_id": policy_id,
            "status": "completed",
            "can_confirm": True,
            "stages": {
                "policy_summary": {"status": "completed", "error_message": None},
                "semantic_audit": {"status": "completed", "error_message": None},
                "conflict_audit": {"status": "completed", "error_message": None},
                "intra_conflict_audit": {"status": "completed", "error_message": None},
            },
            "counts": {
                "clause_total": 4,
                "semantic_ambiguity": 1,
                "executability_gap": 0,
                "duplicate_number": 1,
                "missing_number": 1,
                "mixed_structure": 1,
                "conflict_total": 1,
                "numeric_conflict": 1,
                "authority_conflict": 0,
                "process_conflict": 0,
                "rule_reversal": 0,
                "intra_conflict_total": 1,
                "intra_numeric_conflict": 1,
                "intra_authority_conflict": 0,
                "intra_process_conflict": 0,
                "intra_rule_reversal": 0,
            },
        }

    def get_policy_summary(self, policy_id: str):
        self.get_audit_status(policy_id)
        return {"status": "completed", "error_message": None, "content": {"plain_summary": "概览"}}

    def get_semantic_findings(self, policy_id: str):
        self.get_audit_status(policy_id)
        return {
            "status": "completed",
            "error_message": None,
            "findings": [],
        }

    def get_conflict_findings(self, policy_id: str):
        self.get_audit_status(policy_id)
        return {
            "status": "completed",
            "error_message": None,
            "findings": [{"id": "unit-1", "conflict_type": "numeric_conflict"}],
        }

    def get_intra_conflict_findings(self, policy_id: str):
        self.get_audit_status(policy_id)
        return {
            "status": "completed",
            "error_message": None,
            "findings": [{"id": "unit-1", "conflict_type": "numeric_conflict"}],
        }

    def confirm_policy(self, policy_id: str):
        return {"id": policy_id, "status": "effective"}

    def discard_policy(self, policy_id: str):
        return {"id": policy_id, "status": "discarded"}

    def accept_semantic_audit_result(self, payload):
        return {"audit_id": payload["audit_id"], "status": "completed", "finding_count": 0}

    def accept_conflict_audit_result(self, payload):
        return {"audit_id": payload["audit_id"], "status": "validated", "finding_count": 1}

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

    def list_files(self):
        return {
            "items": [
                {
                    "id": "document-1",
                    "name": "policy.pdf",
                    "file_type": "pdf",
                    "chunk_count": 12,
                }
            ],
            "total": 1,
        }

    def get_file_content(self, file_id: str):
        if file_id != "document-1":
            raise ProofError("file_not_found", "File not found.", status_code=404)
        return {"path": Path(__file__), "name": "policy.pdf", "media_type": "application/pdf"}

    def list_file_chunks(self, file_id: str, *, limit: int = 10, offset: int = 0):
        if file_id != "document-1":
            raise ProofError("file_not_found", "File not found.", status_code=404)
        remaining = max(0, 12 - offset)
        count = min(limit, remaining)
        return {
            "file_id": file_id,
            "items": [
                {
                    "id": f"unit-{offset + index + 1}",
                    "clause_ordinal": offset + index + 1,
                    "content": f"Chunk {offset + index + 1}",
                }
                for index in range(count)
            ],
            "limit": limit,
            "offset": offset,
            "total": 12,
            "has_more": offset + count < 12,
        }

    def index_document(self, document_id: str):
        raise ProofError("embedding_unconfigured", "Embedding API is not configured.", status_code=503)

    def fetch_units(self, unit_ids: list[str]):
        return [
            {
                "id": unit_id,
                "text": f"原文 {unit_id}",
                "citation": {"policy_title": "测试制度", "label": f"[测试制度｜{unit_id}]"},
            }
            for unit_id in unit_ids
        ]

    def search(self, **kwargs):
        raise ProofError("embedding_unconfigured", "Embedding API is not configured.", status_code=503)

    def retrieve_conflict_candidates(self, unit_id: str, *, top_k: int = 10):
        return {
            "source": {"id": unit_id, "text": "报销时限为三十日。"},
            "results": [{"ref": "C01", "id": "unit-2", "text": "报销时限为十五日。"}],
            "candidate_counts": {"returned": top_k},
        }

    def retrieve_intra_conflict_candidates(self, unit_id: str):
        return {
            "source": {
                "id": unit_id, "text": "报销时限为三十日。",
                "clause_no_raw": "第一条", "clause_ordinal": 1,
            },
            "results": [{
                "ref": "C01", "id": "unit-2", "text": "报销时限为十五日。",
                "clause_no_raw": "第二条", "clause_ordinal": 2,
            }],
        }

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
    assert [item["code"] for item in client.get("/v1/categories/levels").json()["data"]] == [
        "upper",
        "peer",
        "lower",
    ]
    assert len(client.get("/v1/categories/policies").json()["data"]) == 3


def test_legacy_category_endpoints_are_removed() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    assert client.get("/v1/meta/policy-levels").status_code == 404
    assert client.get("/v1/categories").status_code == 404
    assert client.post("/v1/categories", json={"code": "risk", "name": "风险管理"}).status_code == 404


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
    assert created.json()["data"]["policy"]["level"]["name"] == "三级制度"
    assert created.json()["data"]["policy"]["category"]["path_name"] == "财务管理"
    assert "level_code" not in created.json()["data"]["policy"]
    assert "level_name" not in created.json()["data"]["policy"]
    assert "category_code" not in created.json()["data"]["policy"]
    assert "category_name" not in created.json()["data"]["policy"]

    failed = client.post("/v1/policies", files={"file": ("policy.txt", b"invalid", "text/plain")})
    assert failed.status_code == 422
    assert failed.json()["details"]["ingestion_run_id"] == "run-failed"


def test_policy_list_and_detail_use_compact_metadata_objects() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    listed = client.get("/v1/policies").json()["data"][0]
    detail = client.get("/v1/policies/policy-1").json()["data"]
    for policy in (listed, detail):
        assert policy["level"] == {"code": "lower", "name": "三级制度", "sort_rank": 100}
        assert policy["category"]["code"] == "finance"
        assert policy["category"]["path_name"] == "财务管理"
        assert not {"level_code", "level_name", "category_code", "category_name"} & policy.keys()


def test_split_audit_result_endpoints() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    status = client.get("/v1/policies/policy-1/audit-status")
    assert status.status_code == 200
    assert status.json()["data"]["stages"]["policy_summary"]["status"] == "completed"
    assert status.json()["data"]["counts"]["semantic_ambiguity"] == 1
    assert status.json()["data"]["counts"]["numeric_conflict"] == 1

    summary = client.get("/v1/policies/policy-1/policy-summary")
    assert summary.json()["data"]["content"]["plain_summary"] == "概览"
    assert status.json()["data"]["stages"]["intra_conflict_audit"]["status"] == "completed"
    assert set(status.json()["data"]["stages"]["intra_conflict_audit"]) == {
        "status", "error_message"
    }
    assert status.json()["data"]["counts"]["intra_numeric_conflict"] == 1

    semantic = client.get("/v1/policies/policy-1/semantic-findings")
    assert "finding_counts" not in semantic.json()["data"]

    conflict = client.get("/v1/policies/policy-1/conflict-findings")
    assert "conflict_counts" not in conflict.json()["data"]
    assert client.get("/v1/policies/missing/audit-status").status_code == 404

    assert client.get("/v1/policies/policy-1/quality-report").status_code == 404
    assert client.post("/v1/policies/policy-1/semantic-audit").status_code == 404
    intra = client.get("/v1/policies/policy-1/intra-conflict-findings")
    assert intra.json()["data"]["findings"][0]["id"] == "unit-1"
    assert set(intra.json()["data"]) == {"status", "error_message", "findings"}



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


def test_file_list_pdf_content_and_limited_chunks() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    listed = client.get("/v1/files")
    assert listed.status_code == 200
    assert listed.json()["data"] == {
        "items": [
            {
                "id": "document-1",
                "name": "policy.pdf",
                "file_type": "pdf",
                "chunk_count": 12,
            }
        ],
        "total": 1,
    }

    content = client.get("/v1/files/document-1/content")
    assert content.status_code == 200
    assert content.headers["content-type"] == "application/pdf"
    assert content.headers["content-disposition"].startswith("inline;")

    chunks = client.get("/v1/files/document-1/chunks?offset=10")
    assert chunks.status_code == 200
    assert [item["id"] for item in chunks.json()["data"]["items"]] == ["unit-11", "unit-12"]
    assert chunks.json()["data"]["has_more"] is False

    assert client.get("/v1/files/document-1/chunks?limit=11").status_code == 422
    assert client.get("/v1/files/missing/content").status_code == 404
    assert client.get("/v1/files/missing/chunks").status_code == 404
    assert client.delete("/v1/files/document-1").status_code == 404


def test_workbench_and_experiment_policy_are_available() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    home = client.get("/")
    assert home.status_code == 200
    assert "Proof 制度工作台" in home.text
    assert "语义歧义、可执行性缺口" in home.text

    workbench = client.get("/workbench")
    assert workbench.status_code == 200
    assert "上传并开始审校" in workbench.text
    assert 'id="categoryParent"' in workbench.text
    assert 'id="repoParentCategoryFilter"' in workbench.text
    assert 'id="repoCategoryFilter"' in workbench.text
    assert "一级分类" in workbench.text
    assert "二级分类" in workbench.text
    assert "确认入库" in workbench.text
    assert "关联制度已删除或不可用" in workbench.text

    experiment = client.get("/examples/policy-semantic-conflict-test.txt")
    assert experiment.status_code == 200
    assert "相关部门应及时处理金额较大的采购事项" in experiment.text
    assert "5000元以下的采购可以在线下直接办理" in experiment.text


def test_sql_query_endpoint() -> None:
    client = TestClient(create_app(Settings(), FakeService()))
    response = client.post(
        "/v1/query/sql",
        json={"question": "有多少份制度？", "sql": "SELECT count(*) AS policy_count FROM proof_sql_policy_v"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["rows"] == [{"policy_count": 85}]


def test_internal_conflict_retrieval_endpoint() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    response = client.post(
        "/v1/internal/conflict-retrieval",
        json={"unit_id": " unit-1 ", "top_k": 7},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert "id" not in data["source"]
    assert data["results"][0]["ref"] == "C01"
    assert "id" not in data["results"][0]
    assert data["candidate_counts"]["returned"] == 7


def test_internal_intra_conflict_retrieval_accepts_only_unit_id() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    response = client.post(
        "/v1/internal/intra-conflict-retrieval",
        json={"unit_id": " unit-1 "},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"]["text"] == "报销时限为三十日。"
    assert "id" not in data["source"]
    assert data["results"][0]["ref"] == "C01"
    assert "id" not in data["results"][0]
    assert client.post(
        "/v1/internal/intra-conflict-retrieval",
        json={"unit_id": "unit-1", "top_k": 3},
    ).status_code == 422


def test_retrieval_fetch_returns_compact_chunks_in_requested_order() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    response = client.post(
        "/v1/retrieval/fetch",
        json={"unit_ids": ["unit-2", "unit-1", "unit-2"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": [
            {
                "id": "unit-2",
                "text": "原文 unit-2",
                "citation": {"policy_title": "测试制度", "label": "[测试制度｜unit-2]"},
            },
            {
                "id": "unit-1",
                "text": "原文 unit-1",
                "citation": {"policy_title": "测试制度", "label": "[测试制度｜unit-1]"},
            },
        ],
    }


def test_internal_conflict_retrieval_defaults_to_ten_results() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    response = client.post(
        "/v1/internal/conflict-retrieval",
        json={"unit_id": "unit-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["candidate_counts"]["returned"] == 10


def test_conflict_agent_view_preserves_service_order_and_removes_noisy_fields() -> None:
    payload = _conflict_agent_view(
        {
            "source": {
                "id": "source",
                "text": "source",
                "level_code": "lower",
                "citation": {"label": "source"},
            },
            "results": [
                {
                    "ref": "C01",
                    "id": "global",
                    "text": "global",
                    "level_code": "upper",
                    "retrieval_sources": ["global"],
                    "branch_ranks": {"global": 1},
                    "rerank_rank": 7,
                    "citation": {"label": "global"},
                    "source_block_ids": ["noise"],
                },
                {
                    "ref": "C02",
                    "id": "same",
                    "text": "same",
                    "retrieval_sources": ["same_title", "leaf_category"],
                    "branch_ranks": {"same_title": 2, "leaf_category": 5},
                    "rerank_rank": 2,
                    "citation": {"label": "same"},
                    "source_block_ids": ["noise"],
                },
            ],
            "candidate_counts": {"returned": 2},
        },
        limit=1,
    )

    assert [item["ref"] for item in payload["results"]] == ["C01"]
    assert "id" not in payload["source"]
    assert "id" not in payload["results"][0]
    assert payload["results"][0]["rerank_rank"] == 7
    assert payload["source"]["level_name"] == "三级制度"
    assert payload["source"]["level_rank"] == 100
    assert payload["results"][0]["level_relation"] == "candidate_is_higher"
    assert payload["policy_level_hierarchy"]["precedence"][0]["code"] == "upper"
    assert "source_block_ids" not in payload["results"][0]
    assert payload["candidate_counts"]["judge_returned"] == 1


def test_review_workflow_endpoints() -> None:
    client = TestClient(create_app(Settings(), FakeService()))

    confirmed = client.post("/v1/policies/policy-1/confirm")
    assert confirmed.json()["data"]["status"] == "effective"
    discarded = client.delete("/v1/policies/policy-1")
    assert discarded.json()["data"]["status"] == "discarded"
    callback = client.post(
        "/v1/internal/semantic-audits/result",
        json={"audit_id": "audit-1", "output": {"summary": {}, "items": []}},
    )
    assert callback.json()["data"]["status"] == "completed"
    assert client.post(
        "/v1/internal/policy-audits/result",
        json={"audit_id": "audit-1", "output": {"summary": {}, "items": []}},
    ).status_code == 404

    conflict_callback = client.post(
        "/v1/internal/conflict-audits/result",
        json={"audit_id": "conflict-1", "output": {"summary": {}, "items": []}},
    )
    assert conflict_callback.json()["data"]["status"] == "validated"
