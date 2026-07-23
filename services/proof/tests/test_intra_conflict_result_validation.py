from __future__ import annotations

import pytest

from proof.application.service import ProofService, _stage_status
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
        self.saved_warnings = []
        self.warning_message = None

    def get_audit_run(self, audit_id):
        return dict(self.run) if audit_id == "audit-1" else None

    def get_audit_run_for_document(self, document_id):
        return dict(self.run) if document_id == "document-1" else None

    def get_document_units(self, document_id):
        return list(self.units) if document_id == "document-1" else []

    def complete_intra_conflict_audit(
        self, audit_id, findings, *, warnings=None, warning_message=None
    ):
        self.saved = findings
        self.saved_warnings = list(warnings or [])
        self.warning_message = warning_message
        self.run["intra_conflict_status"] = "completed"
        self.run["intra_conflict_error_message"] = warning_message

    def mark_intra_conflict_audit_failed(self, audit_id, message):
        self.run["intra_conflict_status"] = "failed"

    def list_intra_conflict_audit_warnings(self, audit_id):
        return list(self.saved_warnings)


class _IntraRetrieval:
    def __init__(self, repository):
        self.repository = repository

    def retrieve_for_unit(self, source_id):
        results = [
            {"ref": f"C{index:02d}", "id": unit["id"]}
            for index, unit in enumerate(
                (unit for unit in self.repository.units if unit["id"] != source_id),
                start=1,
            )
        ]
        return {"source": {"id": source_id}, "results": results}


def _service(repository=None):
    service = object.__new__(ProofService)
    service.repository = repository or _Repository()
    service.intra_conflict_retrieval_service = _IntraRetrieval(service.repository)
    return service


def _finding(source, candidate, conflict_type="numeric_conflict"):
    return {
        "id": source,
        "candidate_ids": [candidate],
        "conflict_type": conflict_type,
        "problem": "同一事项分别规定三十日和十五日，不能同时执行。",
        "suggestion": "统一期限。",
    }


def _ref_finding(candidate_ref="C01", conflict_type="numeric_conflict"):
    return {
        "candidate_refs": [candidate_ref],
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


def _failed_item(target, code="invalid_output"):
    return {
        "status": "failed",
        "input": {"targets": [{"id": target, "unit_id": target}]},
        "error": {"code": code, "message": "model output invalid"},
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

    validation = _service()._validate_intra_conflict_output(payload)

    assert len(validation.findings) == 1
    assert validation.findings[0]["id"] == "unit-a"
    assert validation.findings[0]["candidate_ids"] == ["unit-b"]
    assert validation.warnings == []


def test_short_ref_succeeds_where_one_character_uuid_copy_error_fails():
    source_id = "source-unit"
    true_candidate_id = "92a7abd16ef14c6e93283136a063bb4a"
    corrupted_candidate_id = "92a7abd16ef14c6e83283136a063bb4a"
    repository = _Repository(unit_ids=(source_id, true_candidate_id))

    legacy_payload = _payload([
        _item(source_id, [_finding(source_id, corrupted_candidate_id)]),
        _item(true_candidate_id, []),
    ])
    legacy = _service(repository)._validate_intra_conflict_output(legacy_payload)
    assert legacy.findings == []
    assert legacy.warning_count == 1
    assert legacy.warnings[0]["reason_code"] == "intra_conflict_unit_not_found"

    short_ref_payload = _payload([
        _item(source_id, [_ref_finding("C01")]),
        _item(true_candidate_id, []),
    ])
    short_ref = _service(repository)._validate_intra_conflict_output(short_ref_payload)
    assert short_ref.findings[0]["candidate_ids"] == [true_candidate_id]
    assert short_ref.warnings == []


def test_candidate_refs_are_mapped_to_real_ids_without_model_returning_chunk_ids():
    payload = _payload([
        _item("unit-a", [_ref_finding("C01")]),
        _item("unit-b", [_ref_finding("C01")]),
    ])

    validation = _service()._validate_intra_conflict_output(payload)

    assert validation.findings == [{
        "id": "unit-a",
        "candidate_ids": ["unit-b"],
        "conflict_type": "numeric_conflict",
        "problem": "同一事项分别规定三十日和十五日，不能同时执行。",
        "suggestion": "统一期限。",
    }]
    assert validation.warnings == []


def test_candidate_ref_must_exist_in_current_target_results():
    payload = _payload([
        _item("unit-a", [_ref_finding("C02")]),
        _item("unit-b", []),
    ])

    validation = _service()._validate_intra_conflict_output(payload)

    assert validation.findings == []
    assert validation.warning_count == 1
    assert validation.warning_message == "1 条模型引用无法解析"
    assert validation.warnings[0]["reason_code"] == "intra_conflict_candidate_ref_not_found"


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda finding: finding.update(candidate_ids=["unit-a"]), "invalid_intra_conflict_result"),
        (lambda finding: finding.update(candidate_ids=["unknown"]), "intra_conflict_unit_not_found"),
        (lambda finding: finding.update(candidate_ids=["unit-b", "unit-b"]), "invalid_intra_conflict_result"),
        (lambda finding: finding.update(id="unit-b"), "intra_conflict_target_mismatch"),
    ],
)
def test_intra_conflict_degrades_invalid_ids_to_one_warning(mutate, error_code):
    finding = _finding("unit-a", "unit-b")
    mutate(finding)
    payload = _payload([_item("unit-a", [finding]), _item("unit-b", [])])

    validation = _service()._validate_intra_conflict_output(payload)

    assert validation.findings == []
    assert validation.warning_count == 1
    assert validation.warnings[0]["reason_code"] == error_code



@pytest.mark.parametrize(
    "finding",
    [
        None,
        {},
        {**_ref_finding(), "conflict_type": "unknown"},
        {**_ref_finding(), "problem": ""},
        {**_ref_finding(), "unexpected": True},
    ],
)
def test_any_malformed_finding_becomes_one_unresolved_warning(finding):
    validation = _service()._validate_intra_conflict_output(
        _payload([_item("unit-a", [finding]), _item("unit-b", [])])
    )

    assert validation.findings == []
    assert validation.warning_count == 1
    assert validation.warning_message == "1 条模型引用无法解析"


def test_missing_target_coverage_remains_a_hard_batch_error():
    with pytest.raises(ProofError) as exc_info:
        _service()._validate_intra_conflict_output(
            _payload([_item("unit-a", [])])
        )

    assert exc_info.value.code == "invalid_intra_conflict_result"


def test_single_chunk_document_accepts_empty_findings():
    repository = _Repository(unit_ids=("unit-a",))
    payload = _payload([_item("unit-a", [])])

    validation = _service(repository)._validate_intra_conflict_output(payload)
    assert validation.findings == []
    assert validation.warnings == []


def test_same_ids_with_different_conflict_types_are_not_deduplicated():
    payload = _payload([
        _item("unit-a", [
            _finding("unit-a", "unit-b", "numeric_conflict"),
            _finding("unit-a", "unit-b", "rule_reversal"),
        ]),
        _item("unit-b", []),
    ])

    validation = _service()._validate_intra_conflict_output(payload)

    assert [item["conflict_type"] for item in validation.findings] == [
        "numeric_conflict", "rule_reversal"
    ]


def test_failed_batch_item_becomes_one_warning_without_failing_validation():
    validation = _service()._validate_intra_conflict_output(
        _payload([_failed_item("unit-a"), _item("unit-b", [])])
    )

    assert validation.findings == []
    assert validation.warning_count == 1
    assert validation.warnings[0]["reason_code"] == "invalid_output"


def test_missing_findings_array_becomes_one_warning():
    malformed = _item("unit-a", [])
    malformed["result"] = {"result": {}}

    validation = _service()._validate_intra_conflict_output(
        _payload([malformed, _item("unit-b", [])])
    )

    assert validation.findings == []
    assert validation.warning_count == 1
    assert validation.warning_message == "1 条模型引用无法解析"


def test_intra_stage_completes_with_valid_findings_and_unresolved_warning():
    repository = _Repository()
    proof = _service(repository)
    audit = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    output = _payload([
        _item("unit-a", [_ref_finding("C01"), _ref_finding("C02")]),
        _item("unit-b", []),
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

    assert result == {
        "audit_id": "audit-1",
        "stage_id": "intra_conflict_audit",
        "status": "completed",
        "finding_count": 1,
        "warning_count": 1,
        "warning_message": "1 条模型引用无法解析",
    }
    assert repository.run["intra_conflict_status"] == "completed"
    assert len(repository.saved) == 1
    assert len(repository.saved_warnings) == 1
    state = audit.intra_conflict_state("document-1")
    assert state == {
        "status": "completed",
        "error_message": "1 条模型引用无法解析",
    }
    assert _stage_status(state) == state


def test_intra_failed_stage_completes_with_warning_without_public_shape_change():
    repository = _Repository()
    audit = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)

    result = audit.accept_result(
        {
            "task_type": "proof.audit.run",
            "audit_id": "audit-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "stage_id": "intra_conflict_audit",
            "status": "failed",
            "output": None,
            "error_message": "intra agent timeout",
        }
    )

    assert result["status"] == "completed"
    assert result["finding_count"] == 0
    assert audit.intra_conflict_state("document-1") == {
        "status": "completed",
        "error_message": "intra agent timeout",
    }


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
    assert result["warning_count"] == 0
    assert result["warning_message"] is None
    assert repository.run["intra_conflict_status"] == "completed"
    assert repository.saved[0]["id"] == "unit-a"
