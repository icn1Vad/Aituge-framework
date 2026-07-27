from __future__ import annotations

import builtins
import importlib
import inspect
import sys


def test_formal_direct_runtime_import_does_not_require_fixture_loader(
    monkeypatch,
) -> None:
    """Production images deliberately omit acceptance-only test helpers."""

    module_name = "services.contract.capabilities.direct_runtime"
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
    parameters = inspect.signature(module.execute_direct_bundle).parameters

    assert "value" in parameters
    assert "framework_run_id" in parameters
    assert "allow_dynamic_base_batch_count" in parameters
