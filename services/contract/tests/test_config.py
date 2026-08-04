from __future__ import annotations

from contract.config import Settings


def test_framework_environment_variables_use_frozen_names(monkeypatch) -> None:
    monkeypatch.setenv("FRAMEWORK_BASE_URL", "http://framework-test:8894")
    monkeypatch.setenv("FRAMEWORK_RESULT_SINK_INTERNAL_TOKEN", "callback-token")
    monkeypatch.setenv("FRAMEWORK_STAGE_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("FRAMEWORK_RECOVERY_GRACE_SECONDS", "45")
    monkeypatch.setenv("FRAMEWORK_CONNECT_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("FRAMEWORK_READ_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("REVISION_DRAFT_READ_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("FRAMEWORK_CANCEL_WAIT_SECONDS", "3")

    settings = Settings(_env_file=None)

    assert settings.framework_base_url == "http://framework-test:8894"
    assert settings.framework_result_sink_internal_token == "callback-token"
    assert settings.framework_stage_timeout_seconds == 300
    assert settings.framework_recovery_grace_seconds == 45
    assert settings.framework_connect_timeout_seconds == 2.5
    assert settings.framework_read_timeout_seconds == 12
    assert settings.revision_draft_read_timeout_seconds == 90
    assert settings.framework_cancel_wait_seconds == 3


def test_framework_timeouts_can_be_overridden_in_code() -> None:
    settings = Settings(
        _env_file=None,
        framework_stage_timeout_seconds=1,
        framework_recovery_grace_seconds=0,
    )

    assert settings.framework_stage_timeout_seconds == 1
    assert settings.framework_recovery_grace_seconds == 0


def test_contract_environment_variables_keep_contract_prefix(monkeypatch) -> None:
    monkeypatch.setenv("CONTRACT_INTERNAL_TOKEN", "java-token")
    monkeypatch.setenv("CONTRACT_SCHEMA_VERSION", "1.0")
    monkeypatch.setenv("CONTRACT_DATABASE_MAX_CONNECTIONS", "4")
    monkeypatch.setenv("CONTRACT_DATABASE_CONNECTION_ACQUIRE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("CONTRACT_DATABASE_APPLICATION_NAME", "contract-test")

    settings = Settings(_env_file=None)

    assert settings.internal_token == "java-token"
    assert settings.schema_version == "1.0"
    assert settings.database_max_connections == 4
    assert settings.database_connection_acquire_timeout_seconds == 12.5
    assert settings.database_application_name == "contract-test"
