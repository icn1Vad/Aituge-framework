from __future__ import annotations

import pytest

from proof.application.service import ProofService
from proof.application.semantic_audit import PolicyAuditService
from proof.config import Settings
from proof.errors import ProofError


class _Repository:
    def __init__(self, unit_ids=("unit-a", "unit-b")):
        self.run = {"id": "audit-1", "document_id": "document-1"}
        self.units = [
            {"id": unit_id, "text": unit_id, "clause_ordinal": index}
            for index, unit_id in enumerate(unit_ids, start=1)
        ]
        self.run.update(
            framework_task_id="task-1", framework_run_id="run-1",
            intra_conflict_status="running",
        )
        self.saved = None

    def get_audit_run(self, audit_id):
        return dict(self.run) if audit_id == "audit-1" else None

    def get_document_units(self, document_id):
        return list(self.units) if document_id == "document-1" else []

    def complete_intra_conflict_audit(self, audit_id, findings):
        self.saved = findings
        self.run["intra_conflict_status"] = "completed"

    def mark_intra_conflict_audit_failed(self, audit_id, message):
        self.run["intra_conflict_status"] = "failed"



def _service(repository=None):
    service = object.__new__(ProofService)
    service.repository = repository or _Repository()
    return service


def _finding(source, candidate, conflict_type="numeric_conflict"):
    return {
        "id": source,
        "candidate_ids": [candidate],
        "conflict_type": conflict_type,
        "problem": "同一事项分别规定三十日和十五日，不能同时执行。",
        "suggestion": "统一期限。",
    }


def _item(target, findings):
    return {
        "status": "succeeded",
        "input": {"targets": [{"id": target, "unit_id": target}]},
        "result": {"result": {"findings": findings}},
    }


def _payload(items):
    return {
        "task_type": "proof.audit.run",
        "audit_id": "audit-1",
        "output": {"items": items},
    }


def test_mirror_findings_are_saved_once_with_earliest_clause_as_source():
    payload = _payload([
        _item("unit-a", [_finding("unit-a", "unit-b")]),
        _item("unit-b", [_finding("unit-b", "unit-a")]),
    ])

    findings = _service()._validate_intra_conflict_output(payload)

    assert len(findings) == 1
    assert findings[0]["id"] == "unit-a"
    assert findings[0]["candidate_ids"] == ["unit-b"]


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda finding: finding.update(candidate_ids=["unit-a"]), "invalid_intra_conflict_result"),
        (lambda finding: finding.update(candidate_ids=["unknown"]), "intra_conflict_unit_not_found"),
        (lambda finding: finding.update(candidate_ids=["unit-b", "unit-b"]), "invalid_intra_conflict_result"),
        (lambda finding: finding.update(id="unit-b"), "intra_conflict_target_mismatch"),
    ],
)
def test_intra_conflict_rejects_invalid_ids(mutate, error_code):
    finding = _finding("unit-a", "unit-b")
    mutate(finding)
    payload = _payload([_item("unit-a", [finding]), _item("unit-b", [])])

    with pytest.raises(ProofError) as exc_info:
        _service()._validate_intra_conflict_output(payload)

    assert exc_info.value.code == error_code


def test_single_chunk_document_accepts_empty_findings():
    repository = _Repository(unit_ids=("unit-a",))
    payload = _payload([_item("unit-a", [])])

    assert _service(repository)._validate_intra_conflict_output(payload) == []


def test_same_ids_with_different_conflict_types_are_not_deduplicated():
    payload = _payload([
        _item("unit-a", [
            _finding("unit-a", "unit-b", "numeric_conflict"),
            _finding("unit-a", "unit-b", "rule_reversal"),
        ]),
        _item("unit-b", []),
    ])

    findings = _service()._validate_intra_conflict_output(payload)

    assert [item["conflict_type"] for item in findings] == [
        "numeric_conflict", "rule_reversal"
    ]


def test_intra_stage_uses_existing_parent_task_callback_and_persists_once():
    repository = _Repository()
    proof = _service(repository)
    audit = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    output = _payload([
        _item("unit-a", [_finding("unit-a", "unit-b")]),
        _item("unit-b", [_finding("unit-b", "unit-a")]),
    ])["output"]

    result = audit.accept_result(
        {
            "task_type": "proof.audit.run",
            "audit_id": "audit-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "stage_id": "intra_conflict_audit",
            "status": "completed",
            "output": output,
        },
        intra_conflict_output_validator=proof._validate_intra_conflict_output,
    )

    assert result["stage_id"] == "intra_conflict_audit"
    assert result["finding_count"] == 1
    assert repository.run["intra_conflict_status"] == "completed"
    assert repository.saved[0]["id"] == "unit-a"
