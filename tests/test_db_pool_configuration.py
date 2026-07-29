from __future__ import annotations

import pytest

from db import db_context


def test_postgresql_engine_uses_configured_connection_pool(monkeypatch) -> None:
    monkeypatch.setenv("DB_TYPE", "postgresql")
    monkeypatch.setenv("DB_NAME", "contract_review")
    monkeypatch.setenv("DB_USER", "contract_user")
    monkeypatch.setenv("DB_PASSWORD", "test-password")
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("DB_APPLICATION_NAME", "framework-worker")
    monkeypatch.setenv("HOSTNAME", "pool-test")

    db_context.reset_engine_for_test()
    engine = db_context.get_engine()
    try:
        settings = db_context.get_database_pool_settings()
        assert settings == {
            "pool_size": 3,
            "max_overflow": 1,
            "pool_timeout_seconds": 30,
            "application_name": "framework-worker:pool-test",
        }
        assert engine.sync_engine.pool.size() == 3
        assert engine.sync_engine.pool._max_overflow == 1
        assert db_context.get_database_pool_metrics()["total_checkouts"] == 0
    finally:
        engine.sync_engine.dispose()
        db_context.reset_engine_for_test()


def test_postgresql_pool_size_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "0")

    with pytest.raises(ValueError, match="DB_POOL_SIZE must be >= 1"):
        db_context.get_database_pool_settings()
