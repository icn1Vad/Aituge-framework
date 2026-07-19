from __future__ import annotations

from contract.config import Settings


def test_framework_environment_variables_use_frozen_names(monkeypatch) -> None:
    monkeypatch.setenv("FRAMEWORK_BASE_URL", "http://framework-test:8894")
    monkeypatch.setenv("FRAMEWORK_RESULT_SINK_INTERNAL_TOKEN", "callback-token")

    settings = Settings(_env_file=None)

    assert settings.framework_base_url == "http://framework-test:8894"
    assert settings.framework_result_sink_internal_token == "callback-token"


def test_contract_environment_variables_keep_contract_prefix(monkeypatch) -> None:
    monkeypatch.setenv("CONTRACT_INTERNAL_TOKEN", "java-token")
    monkeypatch.setenv("CONTRACT_SCHEMA_VERSION", "1.0")

    settings = Settings(_env_file=None)

    assert settings.internal_token == "java-token"
    assert settings.schema_version == "1.0"
