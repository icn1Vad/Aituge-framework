from __future__ import annotations

from proof.application.service import ProofService


class _Repository:
    def fetch_units(self, unit_ids: list[str]) -> list[dict]:
        assert unit_ids == ["unit-1"]
        return [
            {
                "id": "unit-1",
                "text": "第一条 测试原文。",
                "policy_id": "policy-1",
                "policy_title": "测试制度",
                "policy_version": "1.0",
                "document_id": "document-1",
                "original_name": "测试制度.pdf",
                "clause_no_raw": "第一条",
                "clause_ordinal": 1,
                "heading_path": ["第一章"],
                "page_start": 2,
                "page_end": 2,
                "source_block_ids": ["internal-block"],
                "text_hash": "internal-hash",
                "embedding_status": "indexed",
                "search_vector": "internal-vector",
            }
        ]


def test_fetch_units_exposes_only_text_and_compact_citation() -> None:
    service = object.__new__(ProofService)
    service.repository = _Repository()

    assert service.fetch_units(["unit-1"]) == [
        {
            "id": "unit-1",
            "text": "第一条 测试原文。",
            "citation": {
                "policy_id": "policy-1",
                "policy_title": "测试制度",
                "policy_version": "1.0",
                "document_id": "document-1",
                "original_name": "测试制度.pdf",
                "clause_no_raw": "第一条",
                "clause_ordinal": 1,
                "heading_path": ["第一章"],
                "page_start": 2,
                "page_end": 2,
                "label": "[测试制度｜第一条｜Chunk #1]",
            },
        }
    ]


class _ConflictRepository:
    def get_policy(self, policy_id: str):
        if policy_id != "policy-source":
            return None
        return {
            "id": policy_id,
            "status": "effective",
            "document_id": "document-source",
        }

    def fetch_units(self, unit_ids: list[str]):
        assert unit_ids == ["candidate-active", "candidate-deleted"]
        return [{"id": "candidate-active"}]


class _ConflictAudit:
    def conflict_state(self, document_id: str):
        assert document_id == "document-source"
        return {"status": "completed", "error_message": None}

    def conflict_findings(self, document_id: str):
        assert document_id == "document-source"
        return [
            {
                "id": "source-unit",
                "candidate_ids": ["candidate-active", "candidate-deleted"],
                "conflict_type": "process_conflict",
                "problem": "流程不一致。",
                "suggestion": "统一流程。",
            }
        ]


def test_conflict_findings_mark_missing_candidates_unavailable_in_one_batch() -> None:
    service = object.__new__(ProofService)
    service.repository = _ConflictRepository()
    service.policy_audit_service = _ConflictAudit()

    result = service._get_conflict_findings("policy-source")

    assert result["status"] == "completed"
    assert result["findings"] == [
        {
            "id": "source-unit",
            "candidate_ids": ["candidate-active", "candidate-deleted"],
            "unavailable_candidate_ids": ["candidate-deleted"],
            "conflict_type": "process_conflict",
            "problem": "流程不一致。",
            "suggestion": "统一流程。",
        }
    ]
