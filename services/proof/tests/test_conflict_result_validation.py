from __future__ import annotations

import pytest

from proof.application.service import ProofService
from proof.application.semantic_audit import PolicyAuditService
from proof.config import Settings
from proof.errors import ProofError


class _Repository:
    def __init__(self) -> None:
        self.units = {
            "source-unit": {
                "id": "source-unit",
                "policy_id": "policy-a",
                "document_id": "document-a",
                "policy_title": "费用报销管理办法",
                "policy_version": "1.0",
                "original_name": "a.docx",
                "clause_no_raw": "第一条",
                "clause_ordinal": 1,
                "heading_path": [],
                "text": "报销申请应当在三十日内提交。",
                "level_code": "lower",
            },
            "candidate-unit": {
                "id": "candidate-unit",
                "policy_id": "policy-b",
                "document_id": "document-b",
                "policy_title": "财务管理制度",
                "policy_version": "1.0",
                "original_name": "b.docx",
                "clause_no_raw": "第二条",
                "clause_ordinal": 2,
                "heading_path": [],
                "text": "报销申请应当在十五日内提交。",
                "level_code": "upper",
            },
        }

    def get_conflict_source_unit(self, unit_id: str):
        return self.units.get(unit_id)


def _service(repository=None) -> ProofService:
    repository = repository or _Repository()
    service = object.__new__(ProofService)
    service.repository = repository
    service.conflict_retrieval_calls = []

    def retrieve(unit_id: str, *, top_k: int = 10):
        service.conflict_retrieval_calls.append((unit_id, top_k))
        source = repository.get_conflict_source_unit(unit_id)
        candidate = repository.get_conflict_source_unit("candidate-unit")
        return {
            "source": dict(source) if source else None,
            "results": [
                {"ref": "C01", **dict(candidate)}
            ] if candidate else [],
            "candidate_counts": {"returned": min(top_k, 1)},
        }

    service.retrieve_conflict_candidates = retrieve
    return service


def _payload() -> dict:
    return {
        "task_type": "proof.conflict.audit",
        "audit_id": "audit-1",
        "output": {
            "items": [
                {
                    "status": "succeeded",
                    "input": {"targets": [{"id": "source-unit", "unit_id": "source-unit"}]},
                    "result": {
                        "result": {
                            "findings": [
                                {
                                    "candidate_refs": ["C01"],
                                    "conflict_type": "numeric_conflict",
                                    "problem": "同一报销申请不能同时按三十日和十五日执行。",
                                    "suggestion": "统一报销申请提交期限。",
                                }
                            ]
                        }
                    },
                }
            ]
        },
    }


def test_conflict_result_sink_maps_short_refs_to_chunk_ids() -> None:
    service = _service()
    result = service.accept_conflict_audit_result(_payload())
    findings = service._validate_conflict_output(_payload())

    assert result == {"audit_id": "audit-1", "status": "validated", "finding_count": 1}
    assert findings[0]["id"] == "source-unit"
    assert findings[0]["candidate_ids"] == ["candidate-unit"]
    assert "candidate_refs" not in findings[0]


def test_multiple_findings_share_one_candidate_mapping_lookup() -> None:
    service = _service()
    payload = _payload()
    first = payload["output"]["items"][0]["result"]["result"]["findings"][0]
    payload["output"]["items"][0]["result"]["result"]["findings"].append(
        {**first, "conflict_type": "process_conflict"}
    )

    findings = service._validate_conflict_output(payload)

    assert len(findings) == 2
    assert service.conflict_retrieval_calls == [("source-unit", 10)]


def test_cross_level_conflict_problem_is_annotated_with_precedence() -> None:
    findings = _service()._validate_conflict_output(_payload())

    assert findings[0]["problem"].startswith(
        "层级关系：当前制度为三级制度，候选 Chunk candidate-unit 为一级制度；"
        "按一级制度 > 二级制度 > 三级制度，一级制度优先。"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda finding: finding.update(id="source-unit"),
        lambda finding: finding.update(candidate_refs=["C02"]),
    ],
)
def test_conflict_result_sink_degrades_invalid_ids_and_unknown_refs(mutation) -> None:
    payload = _payload()
    finding = payload["output"]["items"][0]["result"]["result"]["findings"][0]
    mutation(finding)
    service = _service()

    validation = service._validate_conflict_output(payload)
    result = service.accept_conflict_audit_result(payload)

    assert validation.findings == []
    assert validation.warning_count == 1
    assert validation.warning_message == "1 条模型引用无法解析"
    assert result == {"audit_id": "audit-1", "status": "validated", "finding_count": 0}


class _IntegratedRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.units["source-unit"].update(policy_status="draft")
        self.units["candidate-unit"].update(policy_status="effective")
        self.run = {
            "id": "audit-1",
            "document_id": "document-a",
            "status": "completed",
            "conflict_status": "running",
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
        }
        self.saved_conflicts = None
        self.conflict_error = None

    def get_audit_run(self, audit_id):
        return dict(self.run) if audit_id == self.run["id"] else None

    def get_audit_run_for_document(self, document_id):
        return dict(self.run) if document_id == self.run["document_id"] else None

    def get_document_units(self, document_id):
        if document_id != self.run["document_id"]:
            return []
        return [{"id": "source-unit", "text": self.units["source-unit"]["text"]}]

    def complete_conflict_audit(self, audit_id, findings, *, warning_message=None):
        self.saved_conflicts = findings
        self.run["conflict_status"] = "completed"
        self.conflict_error = warning_message

    def mark_conflict_audit_failed(self, audit_id, message):
        self.conflict_error = message
        self.run["conflict_status"] = "failed"


def _integrated_payload() -> dict:
    payload = _payload()
    payload.update(
        task_type="proof.audit.run",
        task_id="task-1",
        run_id="run-1",
        stage_id="conflict_audit",
        status="completed",
    )
    return payload


def test_integrated_conflict_callback_persists_validated_findings() -> None:
    repository = _IntegratedRepository()
    semantic = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    proof = _service(repository)

    result = semantic.accept_result(
        _integrated_payload(),
        conflict_output_validator=proof._validate_conflict_output,
    )

    assert result == {
        "audit_id": "audit-1",
        "stage_id": "conflict_audit",
        "status": "completed",
        "finding_count": 1,
    }
    assert repository.saved_conflicts[0]["conflict_type"] == "numeric_conflict"
    assert repository.saved_conflicts[0]["candidate_ids"] == ["candidate-unit"]


def test_integrated_conflict_callback_rejects_non_effective_candidate() -> None:
    repository = _IntegratedRepository()
    repository.units["candidate-unit"]["policy_status"] = "draft"
    semantic = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    proof = _service(repository)

    with pytest.raises(ProofError) as exc_info:
        semantic.accept_result(
            _integrated_payload(),
            conflict_output_validator=proof._validate_conflict_output,
        )

    assert exc_info.value.code == "conflict_candidate_not_effective"
    assert repository.saved_conflicts is None
    assert repository.run["conflict_status"] == "failed"


def test_integrated_conflict_stage_failure_is_recorded_separately() -> None:
    repository = _IntegratedRepository()
    semantic = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = _integrated_payload()
    payload.update(status="failed", output=None, error_message="conflict agent timeout")

    result = semantic.accept_result(payload, conflict_output_validator=lambda value: [])

    assert result["status"] == "completed"
    assert result["finding_count"] == 0
    assert repository.run["status"] == "completed"
    assert repository.run["conflict_status"] == "completed"
    assert repository.conflict_error == "conflict agent timeout"
