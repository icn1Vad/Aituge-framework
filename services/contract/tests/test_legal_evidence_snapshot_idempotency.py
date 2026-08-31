from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from contract.config import Settings
from contract.legal_evidence.binding import LegalEvidenceCheckBinder
from contract.legal_evidence.models import (
    FrozenLegalEvidencePlanningFailure,
    LegalEvidenceBundle,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidencePlanSnapshot,
    LegalEvidenceSnapshotCompatibilityError,
    LegalEvidenceSnapshotConflict,
)
from contract.legal_evidence.planner import AdaptiveLegalEvidencePlanner
from contract.legal_evidence.postgres_repository import (
    PostgresLegalEvidenceRepository,
)
from contract.legal_evidence.provider import PlannerLegalEvidenceProvider
from contract.legal_evidence.testing import InMemoryLegalEvidenceRepository
from risk_test_data import risk_plan_input


def _request(*, query: str = "付款法律规则") -> LegalEvidencePlanRequest:
    return LegalEvidencePlanRequest(
        review_id="review-1",
        generation_id="generation-1",
        contract_type="AUTO",
        jurisdiction="CN",
        review_as_of_date=date(2026, 9, 1),
        issues=[
            LegalEvidenceIssue(
                issue_id="legal-issue-" + "1" * 32,
                domain="commercial_financial",
                query=query,
                check_codes=["CF-001"],
            )
        ],
    )


def _bundle(request: LegalEvidencePlanRequest) -> LegalEvidenceBundle:
    raw = AdaptiveLegalEvidencePlanner(InMemoryLegalEvidenceRepository()).plan(request)
    return LegalEvidenceCheckBinder().bind(raw)


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FirstWriterConnection:
    def __init__(self) -> None:
        self.row = None

    def execute(self, sql, params=()):
        if "INSERT INTO legal_evidence_plan_snapshot" in sql:
            if self.row is None:
                if len(params) == 9:
                    self.row = {
                        "request_hash": params[4],
                        "bundle_hash": params[5],
                        "bundle_json": params[6].obj,
                        "status": params[7],
                    }
                else:
                    self.row = {
                        "request_hash": params[3],
                        "bundle_hash": params[4],
                        "bundle_json": params[5].obj,
                        "status": "PLANNER_FAILED",
                    }
            return _Cursor(None)
        if "SELECT request_hash, bundle_hash, bundle_json, status" in sql:
            return _Cursor(self.row)
        raise AssertionError(sql)

    def commit(self):
        return None


class _SnapshotRepository(PostgresLegalEvidenceRepository):
    def __init__(self) -> None:
        super().__init__(Settings())
        self.connection = _FirstWriterConnection()

    @contextmanager
    def _connect(self):
        yield self.connection


class _FailingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, _request):
        self.calls += 1
        raise TimeoutError("temporary planner outage")


class _SuccessfulPlanner:
    def __init__(self, bundle: LegalEvidenceBundle) -> None:
        self.bundle = bundle
        self.calls = 0

    def plan(self, _request):
        self.calls += 1
        return self.bundle


class _RaceRepository:
    def __init__(self, winner: LegalEvidencePlanSnapshot) -> None:
        self.winner = winner

    def load_plan_snapshot(self, **_kwargs):
        return None

    def save_plan_snapshot(self, **_kwargs):
        return self.winner

    def save_failed_plan_snapshot(self, **_kwargs):
        return self.winner


class _FrozenRepository:
    def __init__(self, bundle: LegalEvidenceBundle) -> None:
        self.bundle = bundle

    def load_plan_snapshot(self, *, expected_request_hash: str, **_kwargs):
        return LegalEvidencePlanSnapshot(
            request_hash=expected_request_hash,
            bundle_hash=self.bundle.bundle_hash,
            status=self.bundle.status,
            bundle=self.bundle,
        )

    def save_plan_snapshot(self, **_kwargs):
        raise AssertionError("a frozen snapshot must not be overwritten")

    def save_failed_plan_snapshot(self, **_kwargs):
        raise AssertionError("a frozen snapshot must not be overwritten")


def _failure_snapshot(request_hash: str) -> LegalEvidencePlanSnapshot:
    return LegalEvidencePlanSnapshot(
        request_hash=request_hash,
        bundle_hash="sha256:" + "f" * 64,
        status="PLANNER_FAILED",
        error_type="TimeoutError",
    )


def test_request_hash_is_canonical_and_changes_when_input_changes() -> None:
    first = _request()
    identical = _request()
    changed = _request(query="违约责任法律规则")

    assert first.stable_hash == identical.stable_hash
    assert first.stable_hash != changed.stable_hash


def test_failed_snapshot_is_first_writer_and_success_cannot_replace_it() -> None:
    request = _request()
    repository = _SnapshotRepository()

    failed = repository.save_failed_plan_snapshot(
        request=request,
        attempt_no=1,
        error_type="TimeoutError",
    )
    after_success = repository.save_plan_snapshot(
        request=request,
        bundle=_bundle(request),
        attempt_no=1,
    )

    assert failed.status == "PLANNER_FAILED"
    assert after_success == failed
    assert repository.connection.row["status"] == "PLANNER_FAILED"


def test_success_snapshot_is_first_writer_and_failure_cannot_replace_it() -> None:
    request = _request()
    repository = _SnapshotRepository()
    bundle = _bundle(request)

    success = repository.save_plan_snapshot(
        request=request,
        bundle=bundle,
        attempt_no=1,
    )
    after_failure = repository.save_failed_plan_snapshot(
        request=request,
        attempt_no=1,
        error_type="TimeoutError",
    )

    assert success.bundle == bundle
    assert after_failure == success
    assert repository.connection.row["status"] == bundle.status


def test_same_attempt_with_different_request_hash_is_rejected() -> None:
    repository = _SnapshotRepository()
    first = _request()
    changed = _request(query="违约责任法律规则")
    repository.save_plan_snapshot(
        request=first,
        bundle=_bundle(first),
        attempt_no=1,
    )

    with pytest.raises(LegalEvidenceSnapshotConflict):
        repository.save_plan_snapshot(
            request=changed,
            bundle=_bundle(changed),
            attempt_no=1,
        )


def test_provider_freezes_planner_failure_and_does_not_retry_same_attempt() -> None:
    repository = _SnapshotRepository()
    planner = _FailingPlanner()
    provider = PlannerLegalEvidenceProvider(
        planner=planner,  # type: ignore[arg-type]
        repository=repository,
    )

    with pytest.raises(FrozenLegalEvidencePlanningFailure) as first:
        provider.provide(risk_plan_input())
    with pytest.raises(FrozenLegalEvidencePlanningFailure) as second:
        provider.provide(risk_plan_input())

    assert planner.calls == 1
    assert first.value.error_type == second.value.error_type == "TimeoutError"
    assert str(first.value) == str(second.value)


def test_provider_returns_concurrent_success_that_won_while_planner_failed() -> None:
    value = risk_plan_input()
    raw_bundle = _bundle(_request())
    winner = LegalEvidencePlanSnapshot(
        request_hash=PlannerLegalEvidenceProvider(
            planner=object(),  # type: ignore[arg-type]
            repository=object(),  # type: ignore[arg-type]
        )._request(value).stable_hash,
        bundle_hash=raw_bundle.bundle_hash,
        status=raw_bundle.status,
        bundle=raw_bundle,
    )
    planner = _FailingPlanner()
    provider = PlannerLegalEvidenceProvider(
        planner=planner,  # type: ignore[arg-type]
        repository=_RaceRepository(winner),  # type: ignore[arg-type]
    )

    assert provider.provide(value) == raw_bundle
    assert planner.calls == 1


def test_provider_uses_concurrent_failure_that_won_while_planner_succeeded() -> None:
    value = risk_plan_input()
    request = PlannerLegalEvidenceProvider(
        planner=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
    )._request(value)
    raw_bundle = _bundle(_request())
    planner = _SuccessfulPlanner(raw_bundle)
    provider = PlannerLegalEvidenceProvider(
        planner=planner,  # type: ignore[arg-type]
        repository=_RaceRepository(_failure_snapshot(request.stable_hash)),  # type: ignore[arg-type]
    )

    with pytest.raises(FrozenLegalEvidencePlanningFailure):
        provider.provide(value)
    assert planner.calls == 1


def test_provider_never_rebinds_an_incompatible_frozen_success() -> None:
    value = risk_plan_input()
    incompatible = _bundle(_request()).model_copy(
        update={"binding_profile_version": "legal-check-binding-obsolete"}
    )
    planner = _SuccessfulPlanner(incompatible)
    provider = PlannerLegalEvidenceProvider(
        planner=planner,  # type: ignore[arg-type]
        repository=_FrozenRepository(incompatible),  # type: ignore[arg-type]
    )

    with pytest.raises(LegalEvidenceSnapshotCompatibilityError):
        provider.provide(value)
    assert planner.calls == 0
