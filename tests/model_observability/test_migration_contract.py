from __future__ import annotations

from pathlib import Path

import pytest

from model_observability.entities import (
    ModelInvocationEventEntity,
    ModelInvocationProjectionEntity,
    ModelQuerySnapshotEntity,
    ModelObservabilitySequenceEntity,
)
from model_observability.migrate import (
    ModelObservabilityMigrationError,
    _require_database_url,
    discover_migrations,
)


def test_migrations_have_immutable_up_and_down_pairs() -> None:
    migrations = discover_migrations()
    assert [item.version for item in migrations] == [
        "001_model_invocation_ledger",
        "002_model_observability_hardening",
    ]
    assert all(item.checksum for item in migrations)
    assert migrations[0].down_path.read_text("utf-8").splitlines() == [
        "DROP TABLE IF EXISTS tuge_model_invocation_projection;",
        "DROP TABLE IF EXISTS tuge_model_invocation_event;",
    ]
    hardening_down = migrations[1].down_path.read_text("utf-8")
    assert "DROP TABLE IF EXISTS tuge_model_observability_query_snapshot" in hardening_down
    assert "DROP COLUMN IF EXISTS server_sequence" in hardening_down


def test_postgres_ledger_ddl_enforces_append_and_projection_invariants() -> None:
    ddl = discover_migrations()[0].up_path.read_text("utf-8")
    for required in (
        "CREATE TABLE tuge_model_invocation_event",
        "CREATE TABLE tuge_model_invocation_projection",
        "uq_tuge_model_event_started",
        "uq_tuge_model_event_logical_attempt_started",
        "uq_tuge_model_event_dispatch_conclusion",
        "uq_tuge_model_event_terminal",
        "uq_tuge_model_projection_logical_attempt",
        "ck_tuge_model_projection_terminal",
        "ck_tuge_model_event_cost_metadata",
        "ck_tuge_model_event_dispatch_fact",
        "ck_tuge_model_event_terminal_semantics",
        "ck_tuge_model_projection_time_order",
        "ck_tuge_model_projection_dispatch_outcome",
    ):
        assert required in ddl
    assert "prompt" not in ddl.lower()
    assert "response" not in ddl.lower()
    assert "authorization" not in ddl.lower()


def test_hardening_ddl_adds_database_causality_shared_snapshots_and_fks() -> None:
    ddl = discover_migrations()[1].up_path.read_text("utf-8")
    for required in (
        "CREATE TABLE tuge_model_observability_sequence",
        "server_sequence bigint",
        "uq_tuge_model_event_server_sequence",
        "started_sequence bigint",
        "ck_tuge_model_projection_causal_sequence",
        "MODEL_OBSERVABILITY_PREFLIGHT_PROJECTION_INVALID",
        "ck_tuge_model_event_provider_request_hash",
        "ck_tuge_model_event_metadata_object",
        "ck_tuge_model_event_nonterminal_payload",
        "ck_tuge_model_event_uncertain_usage",
        "ck_tuge_model_event_hash_by_fact",
        "ck_tuge_model_event_ttft_latency",
        "ck_tuge_model_projection_running_payload",
        "ck_tuge_model_projection_uncertain_usage",
        "max_sequence bigint",
        "fk_tuge_model_projection_fallback",
        "trg_tuge_model_event_validate_fallback",
        "MODEL_INVOCATION_FALLBACK_INVALID",
        "DEFERRABLE INITIALLY DEFERRED",
        "trg_tuge_model_event_append_only",
        "CREATE TABLE tuge_model_observability_query_snapshot",
        "idx_tuge_model_snapshot_reuse",
        "idx_tuge_model_snapshot_expiry",
    ):
        assert required in ddl

    assert ddl.index("MODEL_OBSERVABILITY_PREFLIGHT_VALUE_INVALID") < ddl.index(
        "CREATE TABLE tuge_model_observability_sequence"
    )
    assert "DROP CONSTRAINT ck_tuge_model_projection_time_order" not in ddl

def test_migration_runner_requires_an_explicit_postgresql_database() -> None:
    with pytest.raises(ModelObservabilityMigrationError):
        _require_database_url("")
    with pytest.raises(ModelObservabilityMigrationError):
        _require_database_url("sqlite:///unsafe.db")
    assert _require_database_url(
        "postgresql+asyncpg://observer@example.test/model"
    ) == "postgresql://observer@example.test/model"


def test_sqlalchemy_metadata_matches_hardened_postgresql_constraints() -> None:
    event_constraints = {
        item.name for item in ModelInvocationEventEntity.__table__.constraints
    }
    projection_constraints = {
        item.name for item in ModelInvocationProjectionEntity.__table__.constraints
    }
    event_indexes = {item.name for item in ModelInvocationEventEntity.__table__.indexes}
    snapshot_constraints = {
        item.name for item in ModelQuerySnapshotEntity.__table__.constraints
    }

    assert {
        "ck_tuge_model_event_schema",
        "ck_tuge_model_event_server_sequence",
        "ck_tuge_model_event_cost_metadata",
        "ck_tuge_model_event_dispatch_fact",
        "ck_tuge_model_event_terminal_semantics",
        "ck_tuge_model_event_provider_request_hash",
        "ck_tuge_model_event_metadata_object",
        "ck_tuge_model_event_nonterminal_payload",
        "ck_tuge_model_event_uncertain_usage",
        "ck_tuge_model_event_hash_by_fact",
        "ck_tuge_model_event_ttft_latency",
    } <= event_constraints
    assert {
        "ck_tuge_model_projection_started_sequence",
        "ck_tuge_model_projection_dispatch_sequence",
        "ck_tuge_model_projection_terminal_sequence",
        "ck_tuge_model_projection_causal_sequence",
        "ck_tuge_model_projection_cost_metadata",
        "ck_tuge_model_projection_dispatch_outcome",
        "ck_tuge_model_projection_unknown_dispatch",
        "ck_tuge_model_projection_time_order",
        "ck_tuge_model_projection_provider_request_hash",
        "ck_tuge_model_projection_running_payload",
        "ck_tuge_model_projection_uncertain_usage",
        "ck_tuge_model_projection_ttft_latency",
    } <= projection_constraints
    assert {
        "uq_tuge_model_event_started",
        "uq_tuge_model_event_logical_attempt_started",
        "uq_tuge_model_event_dispatch_conclusion",
        "uq_tuge_model_event_terminal",
        "uq_tuge_model_event_server_sequence",
    } <= event_indexes
    assert {
        "ck_tuge_model_snapshot_row_count",
        "ck_tuge_model_snapshot_storage_bytes",
        "ck_tuge_model_snapshot_mode",
        "ck_tuge_model_snapshot_rows_match",
        "ck_tuge_model_snapshot_max_sequence",
    } <= snapshot_constraints
    assert ModelObservabilitySequenceEntity.__tablename__ == (
        "tuge_model_observability_sequence"
    )



def test_event_ledger_has_no_foreign_key_to_rebuildable_projection() -> None:
    assert not ModelInvocationEventEntity.__table__.foreign_keys
    assert {
        foreign_key.target_fullname
        for foreign_key in ModelInvocationProjectionEntity.__table__.foreign_keys
    } == {"tuge_model_invocation_projection.invocation_id"}


def test_checksum_covers_names_up_and_down_with_only_fixed_legacy_exception(
    tmp_path: Path,
) -> None:
    from model_observability.migrate import Migration

    up = tmp_path / "003_example.up.sql"
    down = tmp_path / "003_example.down.sql"
    up.write_text("SELECT 1;\n", "utf-8")
    down.write_text("SELECT 2;\n", "utf-8")
    migration = Migration("003_example", up, down)
    initial = migration.checksum

    down.write_text("SELECT 3;\n", "utf-8")
    assert migration.checksum != initial
    assert migration.accepts_stored_checksum(initial) is False
    assert migration.accepts_stored_checksum("not-a-checksum") is False

    legacy = discover_migrations()[0]
    legacy.assert_source_integrity()
    assert legacy.up_checksum == (
        "a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be"
    )
    assert legacy.checksum == (
        "480f02d221f9f4f4b35bfbce60e1b0ec337e135ae81944cf7955b7b2189a3da8"
    )
    assert legacy.accepts_stored_checksum(
        "a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be"
    )
    assert not discover_migrations()[1].accepts_stored_checksum(
        "a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be"
    )


def test_approved_legacy_pair_rejects_any_current_source_change(
    tmp_path: Path,
) -> None:
    from model_observability.migrate import Migration

    approved = discover_migrations()[0]
    up = tmp_path / approved.up_path.name
    down = tmp_path / approved.down_path.name
    up.write_bytes(approved.up_path.read_bytes())
    down.write_bytes(approved.down_path.read_bytes() + b"-- tampered\n")
    tampered = Migration(approved.version, up, down)

    with pytest.raises(
        ModelObservabilityMigrationError,
        match="source integrity check failed",
    ):
        tampered.assert_source_integrity()
    assert not tampered.accepts_stored_checksum(
        "a3e9768eac735ee3225e65840a2799bc176d6abbca50db5848e49dda5296a2be"
    )
