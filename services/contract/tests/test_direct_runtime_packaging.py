from __future__ import annotations

import builtins
import importlib
import inspect
import sys
from types import SimpleNamespace

import pytest


def test_formal_direct_runtime_import_does_not_require_fixture_loader(
    monkeypatch,
) -> None:
    """Production images deliberately omit acceptance-only test helpers."""

    module_name = "services.contract.scripts.contract_risk_stage66_direct_e2e"
    sys.modules.pop(module_name, None)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "risk_fixture_loader":
            raise AssertionError(
                "formal Direct runtime imported acceptance-only fixture loader"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = importlib.import_module(module_name)
    parameters = inspect.signature(module._execute_one).parameters

    assert "run_id_prefix" in parameters
    assert "allow_dynamic_base_batch_count" in parameters
    assert "diagnostic_allow_oracle_drift" in parameters


def test_partial_review_phase_is_never_finalized_as_success() -> None:
    module = importlib.import_module(
        "services.contract.scripts.contract_risk_stage66_direct_e2e"
    )

    with pytest.raises(module.DirectE2EError) as raised:
        module._require_complete_review_phase(
            "base",
            "PARTIAL_FAILED",
            [
                SimpleNamespace(unit_id="commercial_financial", status="COMPLETED"),
                SimpleNamespace(unit_id="liability_remedies_exit", status="FAILED"),
            ],
        )

    assert raised.value.code == "RISK_REVIEW_PARTIAL_FAILED"
    assert "liability_remedies_exit" in str(raised.value)


def test_complete_review_phase_passes_the_finalization_guard() -> None:
    module = importlib.import_module(
        "services.contract.scripts.contract_risk_stage66_direct_e2e"
    )

    module._require_complete_review_phase(
        "horizontal",
        "COMPLETED",
        [SimpleNamespace(unit_id="cross_clause_consistency", status="COMPLETED")],
    )
