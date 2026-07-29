from __future__ import annotations

import pytest

from contract.config import Settings
from contract.errors import ContractError
from contract.persistence.postgres import connections


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        return None


def _settings(*, max_connections: int = 1, timeout: float = 0.01) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://contract_user:test-password@postgres:5432/contract_review",
        database_max_connections=max_connections,
        database_connection_acquire_timeout_seconds=timeout,
        database_application_name="contract-test",
    )


def test_connection_gate_tags_connections_and_releases_slots(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_connect(url: str, **kwargs):
        calls.append((url, kwargs))
        return _FakeConnection()

    monkeypatch.setattr(connections.psycopg, "connect", fake_connect)
    connections.reset_contract_connection_gates_for_test()
    settings = _settings()

    with connections.open_contract_database_connection(settings) as connection:
        assert isinstance(connection, _FakeConnection)

    assert calls == [
        (
            settings.database_url,
            {
                "connect_timeout": 5,
                "application_name": "contract-test",
            },
        )
    ]


def test_connection_gate_rejects_when_all_contract_slots_are_busy(monkeypatch) -> None:
    monkeypatch.setattr(connections.psycopg, "connect", lambda *_args, **_kwargs: _FakeConnection())
    connections.reset_contract_connection_gates_for_test()
    settings = _settings(max_connections=1, timeout=0.01)

    with connections.open_contract_database_connection(settings):
        with pytest.raises(ContractError) as exc_info:
            with connections.open_contract_database_connection(settings):
                pass

    assert exc_info.value.code == "CONTRACT_DATABASE_CAPACITY_EXCEEDED"
    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable is True
