from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.contract.capabilities.register import (
    _apply_legal_evidence_policy,
    _degrade_or_block_legal_evidence,
    _normalize_legal_evidence_policy,
)
from task_manager.pipeline.errors import StageExecutionError


def _outcome(*, policy: str, usable: bool = False):
    return SimpleNamespace(
        policy=policy,
        usable=usable,
        bundle=object() if usable else None,
        degradation_reasons=[] if usable else ["NO_ACTIVE_RELEASE"],
    )


def test_framework_policy_normalization_is_strict() -> None:
    assert _normalize_legal_evidence_policy(None) == "OFF"
    assert _normalize_legal_evidence_policy("required") == "REQUIRED"
    with pytest.raises(ValueError, match="LEGAL_EVIDENCE_POLICY"):
        _normalize_legal_evidence_policy("best-effort")


def test_optional_policy_keeps_the_explicit_degradation_reason() -> None:
    bundle, reasons = _apply_legal_evidence_policy(
        "OPTIONAL", _outcome(policy="OPTIONAL")
    )
    assert bundle is None
    assert reasons == ["NO_ACTIVE_RELEASE"]
    assert _degrade_or_block_legal_evidence(
        "OPTIONAL", "PLANNING_REQUEST_FAILED"
    ) == ["PLANNING_REQUEST_FAILED"]


def test_required_policy_blocks_unusable_and_failed_planning() -> None:
    with pytest.raises(StageExecutionError, match="NO_ACTIVE_RELEASE"):
        _apply_legal_evidence_policy("REQUIRED", _outcome(policy="REQUIRED"))
    with pytest.raises(StageExecutionError, match="PLANNING_REQUEST_FAILED"):
        _degrade_or_block_legal_evidence("REQUIRED", "PLANNING_REQUEST_FAILED")


def test_required_policy_accepts_only_a_usable_matching_response() -> None:
    outcome = _outcome(policy="REQUIRED", usable=True)
    bundle, reasons = _apply_legal_evidence_policy("REQUIRED", outcome)
    assert bundle is outcome.bundle
    assert reasons == []

    with pytest.raises(ValueError, match="does not match"):
        _apply_legal_evidence_policy("REQUIRED", _outcome(policy="OPTIONAL"))
