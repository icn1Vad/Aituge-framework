from __future__ import annotations

import pytest
from contract.config import Settings
from contract.internal.models import ContractLegalEvidenceRequest
from contract.internal.service import ContractInternalService
from contract.legal_evidence.models import (
    LegalEvidenceBundle,
    LegalEvidenceConflict,
    LegalEvidenceSnapshotConflict,
)
from contract.risk.models import RiskReviewPlanInput


class _Provider:
    def __init__(
        self,
        bundle: LegalEvidenceBundle | None = None,
        error: Exception | None = None,
    ) -> None:
        self.bundle = bundle
        self.error = error
        self.calls = 0

    def provide(self, _value: RiskReviewPlanInput) -> LegalEvidenceBundle | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.bundle


def _plan_input() -> RiskReviewPlanInput:
    return RiskReviewPlanInput.model_construct(
        review_id="review-1",
        document_id="document-1",
        generation_id="generation-1",
        attempt_no=1,
    )


def _unusable_bundle(
    *,
    status: str = "NO_ACTIVE_RELEASE",
    conflicts: bool = False,
) -> LegalEvidenceBundle:
    return LegalEvidenceBundle(
        bundle_hash="sha256:" + "a" * 64,
        status=status,
        release_id=None if status == "NO_ACTIVE_RELEASE" else "release-1",
        issues=[],
        evidence=[],
        relations=[],
        coverage=[],
        unresolved_issue_ids=[],
        conflicts=(
            [
                LegalEvidenceConflict(
                    conflict_type="RELATION",
                    evidence_ids=["legal-evidence-" + "1" * 32],
                    reason="conflicting authority chain",
                )
            ]
            if conflicts
            else []
        ),
        degraded_channels=[],
        stop_reason=(
            "NO_ACTIVE_RELEASE"
            if status == "NO_ACTIVE_RELEASE"
            else "CANDIDATES_EXHAUSTED"
        ),
        examined_candidate_count=0,
        round_count=0,
    )


def _service(provider: _Provider) -> ContractInternalService:
    return ContractInternalService(  # type: ignore[arg-type]
        repository=object(),
        callback_repository=object(),
        legal_evidence_provider=provider,
    )


def test_policy_defaults_off_and_drives_provider_enablement(monkeypatch) -> None:
    assert Settings().legal_evidence_policy == "OFF"
    assert Settings().legal_evidence_enabled is False
    assert Settings(legal_evidence_policy="OPTIONAL").legal_evidence_enabled is True
    assert Settings(legal_evidence_policy="REQUIRED").legal_evidence_enabled is True
    monkeypatch.setenv("LEGAL_EVIDENCE_POLICY", "OPTIONAL")
    assert Settings(_env_file=None).legal_evidence_policy == "OPTIONAL"


def test_off_policy_skips_the_provider_completely() -> None:
    provider = _Provider()
    result = _service(provider).get_legal_evidence(
        ContractLegalEvidenceRequest(policy="OFF", plan_input=_plan_input())
    )
    assert provider.calls == 0
    assert result.enabled is False
    assert result.usable is False
    assert result.bundle is None
    assert result.degradation_reasons == ["POLICY_OFF"]


@pytest.mark.parametrize(
    ("bundle", "reason"),
    [
        (_unusable_bundle(), "BUNDLE_STATUS_NO_ACTIVE_RELEASE"),
        (_unusable_bundle(status="READY"), "EMPTY_CHECK_MAPPING"),
        (
            _unusable_bundle(status="DEGRADED", conflicts=True),
            "CONFLICTS_PRESENT",
        ),
    ],
)
def test_optional_policy_preserves_an_explicit_degradation_reason(
    bundle: LegalEvidenceBundle,
    reason: str,
) -> None:
    result = _service(_Provider(bundle)).get_legal_evidence(
        ContractLegalEvidenceRequest(policy="OPTIONAL", plan_input=_plan_input())
    )
    assert result.usable is False
    assert reason in result.degradation_reasons


@pytest.mark.parametrize(
    "bundle",
    [
        _unusable_bundle(),
        _unusable_bundle(status="READY"),
        _unusable_bundle(status="DEGRADED", conflicts=True),
    ],
)
def test_required_api_marks_unusable_or_unmapped_bundles_explicitly(
    bundle: LegalEvidenceBundle,
) -> None:
    result = _service(_Provider(bundle)).get_legal_evidence(
        ContractLegalEvidenceRequest(policy="REQUIRED", plan_input=_plan_input())
    )
    assert result.usable is False
    assert result.degradation_reasons


def test_internal_api_preserves_snapshot_conflict_as_a_stable_reason() -> None:
    result = _service(
        _Provider(error=LegalEvidenceSnapshotConflict("attempt input changed"))
    ).get_legal_evidence(
        ContractLegalEvidenceRequest(policy="OPTIONAL", plan_input=_plan_input())
    )
    assert result.usable is False
    assert result.degradation_reasons == ["SNAPSHOT_CONFLICT"]
