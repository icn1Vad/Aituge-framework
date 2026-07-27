from proof.tools.reprocess_policy_audits import reprocess_policy_audits


class FakeRepository:
    def __init__(self) -> None:
        self.files = [
            {"id": "complete", "policy_id": "policy-complete"},
            {"id": "running", "policy_id": "policy-running"},
            {"id": "partial", "policy_id": "policy-partial"},
            {"id": "missing", "policy_id": "policy-missing"},
        ]
        self.runs = {
            "complete": {
                "status": "completed",
                "summary_status": "completed",
                "conflict_status": "completed",
            },
            "running": {
                "status": "completed",
                "summary_status": "running",
                "conflict_status": "pending",
            },
            "partial": {
                "status": "completed",
                "summary_status": "failed",
                "conflict_status": "pending",
            },
        }

    def list_files(self):
        return self.files

    def get_audit_run_for_document(self, document_id):
        return self.runs.get(document_id)


class FakeAuditService:
    def __init__(self) -> None:
        self.dispatched = []

    def ensure_dispatched(self, document_id):
        self.dispatched.append(document_id)
        return {
            "status": "running",
            "policy_summary": {"status": "running"},
            "conflict_audit": {"status": "running"},
        }


def test_dry_run_selects_only_incomplete_idle_documents() -> None:
    repository = FakeRepository()
    service = FakeAuditService()

    result = reprocess_policy_audits(service, repository, dry_run=True)

    assert result["candidate_count"] == 2
    assert result["skipped_complete_count"] == 1
    assert result["skipped_running_count"] == 1
    assert [item["document_id"] for item in result["items"]] == ["partial", "missing"]
    assert service.dispatched == []


def test_reprocess_dispatches_selected_history() -> None:
    repository = FakeRepository()
    service = FakeAuditService()

    result = reprocess_policy_audits(service, repository, limit=1)

    assert result["selected_count"] == 1
    assert result["dispatched_count"] == 1
    assert service.dispatched == ["partial"]
