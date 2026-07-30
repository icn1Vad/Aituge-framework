from __future__ import annotations

import pytest

from proof.application.semantic_audit import PolicyAuditService
from proof.config import Settings
from proof.errors import ProofError
from proof.tenant import tenant_scope
from proof.model_pack import model_pack_scope


class FakeAuditRepository:
    def __init__(self) -> None:
        self.units = [
            {
                "id": "unit-1",
                "text": "第一条 相关部门应及时处理。",
                "clause_no_raw": "第一条",
                "clause_ordinal": 1,
                "heading_path": ["第一章"],
            },
            {
                "id": "unit-2",
                "text": "第二条 财务部应在三个工作日内完成复核。",
                "clause_no_raw": "第二条",
                "clause_ordinal": 2,
                "heading_path": ["第一章"],
            },
        ]
        self.run = {
            "id": "audit-1",
            "document_id": "document-1",
            "status": "running",
            "summary_status": "running",
            "conflict_status": "running",
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
            "error_message": None,
        }
        self.saved_findings = None

    def get_audit_run(self, audit_id):
        return dict(self.run) if self.run and audit_id == self.run["id"] else None

    def get_audit_run_for_document(self, document_id):
        return dict(self.run) if self.run and document_id == self.run["document_id"] else None

    def create_audit_run(self, *, audit_run_id, document_id):
        self.run = {
            "id": audit_run_id,
            "document_id": document_id,
            "status": "pending",
            "summary_status": "pending",
            "conflict_status": "pending",
            "framework_task_id": None,
            "framework_run_id": None,
            "error_message": None,
        }
        return dict(self.run)

    def prepare_audit_run_for_dispatch(self, audit_id):
        if not self.run or self.run["id"] != audit_id:
            return False
        statuses = (
            self.run["status"],
            self.run["summary_status"],
            self.run["conflict_status"],
        )
        if all(status == "completed" for status in statuses):
            return False
        if self.run["status"] != "completed":
            self.run.update(status="running", error_message=None)
            self.saved_findings = None
        if self.run["summary_status"] != "completed":
            self.run["summary_status"] = "running"
        if self.run["conflict_status"] != "completed":
            self.run["conflict_status"] = "running"
        if self.run.get("intra_conflict_status") != "completed":
            self.run["intra_conflict_status"] = "running"
        self.run.update(framework_task_id=None, framework_run_id=None)
        return True

    def get_document_units(self, document_id):
        return list(self.units) if document_id == self.run["document_id"] else []

    def complete_audit(self, audit_id, findings, *, warning_message=None):
        self.saved_findings = findings
        self.run["status"] = "completed"
        self.run["error_message"] = warning_message

    def mark_audit_failed(self, audit_id, message):
        if self.run["status"] != "completed":
            self.run["status"] = "failed"
            self.run["error_message"] = message

    def complete_audit_summary(
        self, audit_id, content, *, warning_message=None
    ):
        self.run["summary_status"] = "completed"
        self.run["summary_content"] = content
        self.run["summary_error_message"] = warning_message

    def mark_audit_summary_failed(self, audit_id, message):
        if self.run["summary_status"] != "completed":
            self.run["summary_status"] = "failed"

    def complete_conflict_audit(
        self, audit_id, findings, *, warning_message=None
    ):
        self.run["conflict_status"] = "completed"
        self.run["conflict_error_message"] = warning_message

    def mark_conflict_audit_failed(self, audit_id, message):
        if self.run["conflict_status"] != "completed":
            self.run["conflict_status"] = "failed"

    def complete_intra_conflict_audit(
        self, audit_id, findings, *, warnings=None, warning_message=None
    ):
        self.run["intra_conflict_status"] = "completed"
        self.run["intra_conflict_error_message"] = warning_message

    def mark_intra_conflict_audit_failed(self, audit_id, message):
        if self.run.get("intra_conflict_status") != "completed":
            self.run["intra_conflict_status"] = "failed"

    def list_audit_findings(self, audit_id):
        return self.saved_findings or []


def callback_payload() -> dict:
    return {
        "audit_id": "audit-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "stage_id": "semantic_audit",
        "output": {
            "summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            "items": [
                {
                    "status": "succeeded",
                    "input": {
                        "targets": [
                            {"id": "unit-1", "ref": "T01"},
                            {"id": "unit-2", "ref": "T02"},
                        ]
                    },
                    "result": {
                        "result": {
                            "findings": [
                                {
                                    "target_ref": "T01",
                                    "category": "semantic_ambiguity",
                                    "problem": "责任主体和完成时限不明确。",
                                    "suggestion": "明确责任部门和处理时限。",
                                }
                            ]
                        }
                    },
                }
            ],
        },
    }


def test_semantic_callback_validates_and_atomically_completes() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)

    result = service.accept_result(callback_payload())

    assert result == {
        "audit_id": "audit-1",
        "stage_id": "semantic_audit",
        "status": "completed",
        "finding_count": 1,
    }
    assert repository.saved_findings == [
        {
            "id": "unit-1",
            "category": "semantic_ambiguity",
            "problem": "责任主体和完成时限不明确。",
            "suggestion": "明确责任部门和处理时限。",
        }
    ]


def test_semantic_callback_maps_target_ref_to_matching_real_chunk_id() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload["output"]["items"][0]["result"]["result"]["findings"][0]["target_ref"] = "T02"

    service.accept_result(payload)

    assert repository.saved_findings[0]["id"] == "unit-2"
    assert "target_ref" not in repository.saved_findings[0]


def test_semantic_callback_degrades_model_supplied_chunk_id_to_warning() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload["output"]["items"][0]["result"]["result"]["findings"][0]["id"] = "unit-1"

    result = service.accept_result(payload)

    assert result["status"] == "completed"
    assert result["finding_count"] == 0
    assert repository.run["status"] == "completed"
    assert repository.run["error_message"] == "1 条模型引用无法解析"
    assert repository.saved_findings == []


def test_semantic_callback_degrades_ref_outside_current_item_to_warning() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload["output"]["items"][0]["result"]["result"]["findings"][0]["target_ref"] = "T08"

    result = service.accept_result(payload)

    assert result["status"] == "completed"
    assert result["finding_count"] == 0
    assert repository.run["error_message"] == "1 条模型引用无法解析"
    assert repository.saved_findings == []


def test_semantic_failed_stage_completes_with_warning() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload.update(status="failed", output=None, error_message="semantic agent timeout")

    result = service.accept_result(payload)

    assert result["status"] == "completed"
    assert result["finding_count"] == 0
    assert repository.run["status"] == "completed"
    assert repository.run["error_message"] == "semantic agent timeout"


def test_summary_failed_stage_completes_with_fallback_and_warning() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload.update(
        stage_id="policy_summary", status="failed", output=None,
        error_message="summary agent timeout",
    )

    result = service.accept_result(payload)

    assert result["status"] == "completed"
    assert repository.run["summary_status"] == "completed"
    assert repository.run["summary_error_message"] == "summary agent timeout"
    assert repository.run["summary_content"]["plain_summary"] == "模型摘要无法解析。"


def test_callback_identity_mismatch_does_not_poison_audit_state() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload["task_id"] = "stale-task"

    with pytest.raises(ProofError) as exc_info:
        service.accept_result(payload)

    assert exc_info.value.code == "invalid_audit_result"
    assert repository.run["status"] == "running"
    assert repository.run["error_message"] is None


def test_semantic_missing_chunk_coverage_remains_hard_failure() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = callback_payload()
    payload["output"]["items"][0]["input"]["targets"].pop()

    with pytest.raises(ProofError) as exc_info:
        service.accept_result(payload)

    assert exc_info.value.code == "invalid_audit_result"
    assert repository.run["status"] == "failed"


def test_semantic_items_preserve_batching_and_assign_short_refs() -> None:
    repository = FakeAuditRepository()
    settings = Settings(
        semantic_audit_enabled=True,
        audit_batch_max_chars=1000,
        audit_batch_max_chunks=8,
        audit_max_chunk_chars=100,
    )
    service = PolicyAuditService(settings, repository)

    batches = service._build_semantic_items("audit-1", repository.units)

    assert [[target["id"] for target in item["targets"]] for item in batches] == [
        ["unit-1", "unit-2"],
    ]
    assert [target["ref"] for target in batches[0]["targets"]] == ["T01", "T02"]


def test_semantic_items_split_after_eight_targets_and_reset_refs() -> None:
    repository = FakeAuditRepository()
    repository.units = [
        {
            "id": f"unit-{index}",
            "text": f"第{index}条 测试条款。",
            "clause_no_raw": f"第{index}条",
            "clause_ordinal": index,
            "heading_path": [],
        }
        for index in range(1, 10)
    ]
    service = PolicyAuditService(
        Settings(
            semantic_audit_enabled=True,
            audit_batch_max_chars=10_000,
            audit_batch_max_chunks=8,
        ),
        repository,
    )

    items = service._build_semantic_items("audit-1", repository.units)

    assert [len(item["targets"]) for item in items] == [8, 1]
    assert [target["ref"] for target in items[0]["targets"]] == [
        "T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08"
    ]
    assert items[1]["targets"][0]["ref"] == "T01"


def test_conflict_items_cover_each_chunk_exactly_once() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)

    items = service._build_conflict_items("audit-1", repository.units)

    assert [item["targets"][0]["id"] for item in items] == ["unit-1", "unit-2"]
    assert all(len(item["targets"]) == 1 for item in items)
    assert all(item["targets"][0]["id"] == item["targets"][0]["unit_id"] for item in items)


def test_intra_conflict_items_cover_each_chunk_exactly_once() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)

    items = service._build_intra_conflict_items("audit-1", repository.units)

    assert [item["targets"][0]["id"] for item in items] == ["unit-1", "unit-2"]
    assert all(len(item["targets"]) == 1 for item in items)
    assert all(item["id"].startswith("audit-1:intra-conflict:") for item in items)


def test_policy_summary_accepts_compact_identifier_free_outline() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    output = {
        "plain_summary": "一、制度定位与总体框架\n制度用于规范事项处理。",
        "purpose": "明确管理要求。",
        "scope": ["公司相关业务。"],
        "concerned_roles": [
            {"role": "财务部", "summary": "负责复核相关事项并记录处理结果。"}
        ],
        "key_rules": ["复核应在三个工作日内完成。"],
    }

    assert service._validate_summary(repository.run, output) == output


def test_policy_summary_rejects_legacy_chunk_identifier_shape() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    output = {
        "plain_summary": "制度概览",
        "purpose": {"text": "明确管理要求。", "source_ids": ["unit-1"]},
        "scope": [],
        "concerned_roles": [],
        "key_rules": [],
    }

    with pytest.raises(ValueError, match="purpose must be null or a non-blank string"):
        service._validate_summary(repository.run, output)


def test_policy_summary_rejects_removed_key_process_field() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    output = {
        "plain_summary": "制度概览",
        "purpose": None,
        "scope": [],
        "concerned_roles": [],
        "key_process": [],
        "key_rules": [],
    }

    with pytest.raises(ValueError, match=r"unknown fields: \['key_process'\]"):
        service._validate_summary(repository.run, output)


def test_policy_summary_rejects_removed_exceptions_field() -> None:
    repository = FakeAuditRepository()
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    output = {
        "plain_summary": "制度概览",
        "purpose": None,
        "scope": [],
        "concerned_roles": [],
        "key_rules": [],
        "exceptions": [],
    }

    with pytest.raises(ValueError, match=r"unknown fields: \['exceptions'\]"):
        service._validate_summary(repository.run, output)


def test_disabled_audit_has_no_run_and_is_confirmable_by_caller() -> None:
    repository = FakeAuditRepository()
    repository.run = None
    repository.get_audit_run_for_document = lambda document_id: None
    service = PolicyAuditService(Settings(semantic_audit_enabled=False), repository)

    assert service.ensure_dispatched("document-1") == {
        "status": "disabled",
        "error_message": None,
    }


def test_completed_semantic_result_is_preserved_while_missing_stages_are_redispatched(monkeypatch) -> None:
    repository = FakeAuditRepository()
    repository.run.update(
        status="completed",
        summary_status="pending",
        conflict_status="pending",
    )
    repository.saved_findings = [{"id": "unit-1", "category": "semantic_ambiguity"}]
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    dispatched = []
    monkeypatch.setattr(service, "_dispatch", lambda run: dispatched.append(dict(run)))

    state = service.ensure_dispatched("document-1")

    assert len(dispatched) == 1
    assert dispatched[0]["status"] == "completed"
    assert dispatched[0]["summary_status"] == "running"
    assert dispatched[0]["conflict_status"] == "running"
    assert repository.saved_findings == [{"id": "unit-1", "category": "semantic_ambiguity"}]
    assert state["status"] == "completed"
    assert state["policy_summary"]["status"] == "running"
    assert state["conflict_audit"]["status"] == "running"


def test_audit_is_claimed_before_slow_dispatch(monkeypatch) -> None:
    repository = FakeAuditRepository()
    repository.run = None
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    dispatched = []
    monkeypatch.setattr(service, "_dispatch", lambda run: dispatched.append(dict(run)))

    first = service.ensure_dispatched("document-1")
    second = service.ensure_dispatched("document-1")

    assert len(dispatched) == 1
    assert first["status"] == "running"
    assert second["status"] == "running"


def test_final_callback_completes_when_all_model_stages_are_degraded() -> None:
    repository = FakeAuditRepository()
    repository.run.update(
        status="running",
        summary_status="running",
        conflict_status="running",
        intra_conflict_status="running",
    )
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    stages = {
        stage_id: {"status": "failed", "error_message": f"{stage_id} failed"}
        for stage_id in (
            "policy_summary", "semantic_audit", "conflict_audit",
            "intra_conflict_audit",
        )
    }

    result = service.accept_result(
        {
            "audit_id": "audit-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "task_type": "proof.audit.run",
            "stage_id": "finalize_report",
            "status": "completed",
            "output": {"artifacts": {}, "stages": stages},
        }
    )

    assert result["status"] == "completed"
    assert repository.run["summary_status"] == "completed"
    assert repository.run["status"] == "completed"
    assert repository.run["conflict_status"] == "completed"
    assert repository.run["intra_conflict_status"] == "completed"
    assert repository.run["error_message"] == "semantic_audit failed"


def test_backfill_pipeline_failure_preserves_semantic_and_fails_missing_stages() -> None:
    repository = FakeAuditRepository()
    repository.run.update(
        status="completed",
        summary_status="running",
        conflict_status="running",
    )
    service = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)

    result = service.accept_result(
        {
            "audit_id": "audit-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "task_type": "proof.audit.run",
            "stage_id": "finalize_report",
            "status": "failed",
            "error_message": "pipeline failed",
            "output": None,
        },
        conflict_output_validator=lambda payload: [],
    )

    assert result["status"] == "failed"
    assert repository.run["status"] == "completed"
    assert repository.run["summary_status"] == "failed"
    assert repository.run["conflict_status"] == "failed"


def test_dispatch_failure_is_recorded_without_raising(monkeypatch) -> None:
    repository = FakeAuditRepository()
    repository.run = None
    service = PolicyAuditService(
        Settings(semantic_audit_enabled=True, framework_base_url="http://framework.test"),
        repository,
    )
    monkeypatch.setattr(service, "_dispatch", lambda run: (_ for _ in ()).throw(RuntimeError("down")))

    state = service.ensure_dispatched("document-1")

    assert state["status"] == "failed"
    assert state["error_message"] == "down"


def test_framework_task_headers_follow_current_tenant() -> None:
    service = PolicyAuditService(
        Settings(semantic_audit_enabled=True),
        FakeAuditRepository(),
    )

    with tenant_scope("1"), model_pack_scope("api-rerank"):
        main_headers = service._headers()
    with tenant_scope("2"), model_pack_scope("local-rerank"):
        demo_headers = service._headers()

    assert main_headers["X-Tenant-ID"] == "1"
    assert demo_headers["X-Tenant-ID"] == "2"
    assert main_headers["X-Model-Pack-ID"] == "api-rerank"
    assert demo_headers["X-Model-Pack-ID"] == "local-rerank"
    assert main_headers["X-User-ID"] == demo_headers["X-User-ID"] == "proof-service"
