from __future__ import annotations

from contextlib import contextmanager

import pytest
from contract.config import Settings
from contract.legal_evidence.indexer import PROJECTION_VERSION, LegalEvidenceIndexer
from contract.legal_evidence.postgres_repository import PostgresLegalEvidenceRepository
from contract.persistence.postgres.migrate import MIGRATIONS_DIR


class _Cursor:
    def __init__(self, *, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _ConnectionRepository(PostgresLegalEvidenceRepository):
    def __init__(self, connection) -> None:
        super().__init__(Settings())
        self.connection = connection

    @contextmanager
    def _connect(self):
        yield self.connection


class _StageConnection:
    def __init__(self, *, manifest: str = "a" * 64) -> None:
        self.manifest = manifest
        self.statements: list[str] = []
        self.commit_count = 0

    def execute(self, sql, _params):
        self.statements.append(sql)
        if "INSERT INTO legal_evidence_release" in sql:
            return _Cursor(row=None)
        if "SELECT source_release_id" in sql:
            return _Cursor(
                row={
                    "source_release_id": "source-1",
                    "source_manifest_sha256": self.manifest,
                    "projection_version": PROJECTION_VERSION,
                    "embedding_profile_id": None,
                    "status": "STAGED",
                }
            )
        return _Cursor()

    def commit(self) -> None:
        self.commit_count += 1


def test_staging_an_existing_staged_release_explicitly_reopens_building_state() -> None:
    connection = _StageConnection()

    writable = _ConnectionRepository(connection).stage_release(
        release_id="release-1",
        source_release_id="source-1",
        source_manifest_sha256="a" * 64,
        projection_version=PROJECTION_VERSION,
        embedding_profile_id=None,
    )

    assert writable is True
    reset = next(sql for sql in connection.statements if "SET projection_status" in sql)
    assert "projection_status = 'BUILDING'" in reset
    assert "projected_unit_count = 0" in reset
    assert "projected_relation_count = 0" in reset
    assert connection.commit_count == 1


def test_staging_rejects_release_id_reuse_with_different_manifest() -> None:
    connection = _StageConnection(manifest="b" * 64)

    with pytest.raises(RuntimeError, match="different publication inputs"):
        _ConnectionRepository(connection).stage_release(
            release_id="release-1",
            source_release_id="source-1",
            source_manifest_sha256="a" * 64,
            projection_version=PROJECTION_VERSION,
            embedding_profile_id=None,
        )

    assert not any("SET projection_status" in sql for sql in connection.statements)
    assert connection.commit_count == 0


def _release_count_row(*, status: str = "STAGED", unit_count: int = 2) -> dict:
    return {
        "status": status,
        "projection_status": "READY",
        "embedding_profile_id": None,
        "projected_unit_count": unit_count,
        "projected_relation_count": 1,
        "unit_count": unit_count,
        "relation_count": 1,
        "embedding_count": 0,
    }


class _ActivationConnection:
    def __init__(self, *, status: str) -> None:
        self.row = _release_count_row(status=status)
        self.statements: list[str] = []
        self.commit_count = 0

    def execute(self, sql, _params):
        self.statements.append(sql)
        if "SELECT r.status" in sql:
            return _Cursor(row=self.row)
        return _Cursor()

    def commit(self) -> None:
        self.commit_count += 1


def test_activation_of_current_release_is_a_true_idempotent_noop() -> None:
    connection = _ActivationConnection(status="ACTIVE")

    _ConnectionRepository(connection).activate_release("release-active")

    assert not any("UPDATE legal_evidence_release" in sql for sql in connection.statements)
    assert connection.commit_count == 0


def test_retired_release_can_be_reactivated_for_rollback() -> None:
    sql = (MIGRATIONS_DIR / "006_legal_evidence_projection.sql").read_text("utf-8")
    connection = _ActivationConnection(status="RETIRED")

    _ConnectionRepository(connection).activate_release("release-retired")

    assert "OLD.status = 'RETIRED' AND NEW.status = 'ACTIVE'" in sql
    updates = [
        statement
        for statement in connection.statements
        if statement.lstrip().startswith("UPDATE")
    ]
    assert len(updates) == 2
    assert "status = 'RETIRED'" in updates[0]
    assert "status = 'ACTIVE'" in updates[1]
    assert connection.commit_count == 1


class _SealConnection:
    def __init__(
        self,
        *,
        unit_count: int,
        embedding_count: int = 0,
        projection_status: str = "BUILDING",
    ) -> None:
        self.row = {
            "status": "STAGED",
            "projection_status": projection_status,
            "embedding_profile_id": None,
            "projected_unit_count": unit_count,
            "projected_relation_count": 3,
            "unit_count": unit_count,
            "relation_count": 3,
            "embedding_count": embedding_count,
        }
        self.statements: list[str] = []
        self.commit_count = 0

    def execute(self, sql, _params):
        self.statements.append(sql)
        if "SELECT r.status" in sql:
            return _Cursor(row=self.row)
        return _Cursor()

    def commit(self) -> None:
        self.commit_count += 1


def test_seal_uses_database_count_and_rejects_expected_count_mismatch() -> None:
    connection = _SealConnection(unit_count=2)

    with pytest.raises(RuntimeError, match="expected 3 units, found 2"):
        _ConnectionRepository(connection).mark_projection_complete(
            "release-1", expected_unit_count=3
        )

    assert not any("SET projection_status = 'READY'" in sql for sql in connection.statements)
    assert connection.commit_count == 0


def test_seal_records_authoritative_database_counts() -> None:
    connection = _SealConnection(unit_count=2)

    counts = _ConnectionRepository(connection).mark_projection_complete(
        "release-1", expected_unit_count=2
    )

    assert counts == (2, 3)
    assert any("SET projection_status = 'READY'" in sql for sql in connection.statements)
    assert connection.commit_count == 1


def test_repeated_seal_of_unchanged_ready_projection_is_idempotent() -> None:
    connection = _SealConnection(unit_count=2, projection_status="READY")

    counts = _ConnectionRepository(connection).mark_projection_complete(
        "release-1", expected_unit_count=2
    )

    assert counts == (2, 3)
    assert not any("SET projection_status = 'READY'" in sql for sql in connection.statements)
    assert connection.commit_count == 0


def test_failed_projection_must_be_reopened_by_stage_before_sealing() -> None:
    connection = _SealConnection(unit_count=2, projection_status="FAILED")

    with pytest.raises(RuntimeError, match="Only a BUILDING"):
        _ConnectionRepository(connection).mark_projection_complete(
            "release-1", expected_unit_count=2
        )

    assert connection.commit_count == 0


def test_sealed_projection_children_are_immutable_until_explicitly_reopened() -> None:
    sql = (MIGRATIONS_DIR / "006_legal_evidence_projection.sql").read_text("utf-8")

    assert "old_projection_status = 'READY'" in sql
    assert "new_projection_status = 'READY'" in sql
    assert "sealed or published legal evidence projection rows are immutable" in sql


class _AmbiguousArticleConnection:
    def __init__(self, *, bulk: bool) -> None:
        self.bulk = bulk
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        has_version_gate = "HAVING count(DISTINCT" in sql and "unique_version" in sql
        if self.bulk:
            return _Cursor(
                rows=[
                    {
                        "instrument_id": "instrument-1",
                        "article_no": "第一条",
                        # A real PostgreSQL query produces no unit when the
                        # instrument has multiple versions and the gate exists.
                        "unit_ids": None if has_version_gate else ["unit-v1"],
                    }
                ]
            )
        return _Cursor(rows=[] if has_version_gate else [{"unit_id": "unit-v1"}])


def test_single_article_resolution_rejects_instrument_version_ambiguity() -> None:
    connection = _AmbiguousArticleConnection(bulk=False)

    resolved = _ConnectionRepository(connection).unit_id_by_instrument_article(
        "release-1", "instrument-1", "第一条"
    )

    assert resolved is None
    assert "HAVING count(DISTINCT version_id) = 1" in connection.sql
    assert connection.params == (
        "release-1",
        "instrument-1",
        "release-1",
        "instrument-1",
        "第一条",
    )


def test_bulk_article_resolution_rejects_instrument_version_ambiguity() -> None:
    connection = _AmbiguousArticleConnection(bulk=True)

    resolved = _ConnectionRepository(connection).unit_ids_by_instrument_articles(
        "release-1", {("instrument-1", "第一条")}
    )

    assert resolved == {("instrument-1", "第一条"): None}
    assert "HAVING count(DISTINCT u.version_id) = 1" in connection.sql


class _CountedSource:
    def __init__(self, expected_count: int) -> None:
        self.expected_count = expected_count

    def active_release(self):
        return {"release_id": "source-1", "manifest_sha256": "a" * 64}

    def retrieval_root_count(self, _release_id):
        return self.expected_count

    def iter_nodes(self, _release_id, *, page_size):
        del page_size
        yield {
            "release_id": "source-1",
            "instrument_key": "instrument-1",
            "version_id": "version-1",
            "title": "测试法规",
            "jurisdiction_code": "CN",
            "jurisdiction_name": "中国",
            "issuing_authority_names_json": "[]",
            "source_url": None,
            "node_id": "preamble-1",
            "parent_node_id": None,
            "node_type": "PREAMBLE",
            "node_number": None,
            "heading": None,
            "content_plain": "制定本法规。",
            "sequence": 1,
            "path": "前言",
            "content_sha256": "b" * 64,
        }

    def iter_instruments(self, _release_id, *, page_size):
        del page_size
        return iter(())

    def iter_relations(self, _release_id, *, page_size):
        del page_size
        return iter(())


class _CountedTarget:
    def __init__(self) -> None:
        self.units = []
        self.expected_count = None
        self.stage_projection_version = None

    def stage_release(self, *, projection_version, **_kwargs):
        self.stage_projection_version = projection_version
        return True

    def upsert_units(self, units):
        self.units.extend(units)
        return len(units)

    def representative_unit_ids(self, _release_id):
        return {}

    def iter_units(self, _release_id, *, page_size):
        del page_size
        return iter(self.units)

    def unit_ids_by_instrument_articles(self, _release_id, _keys):
        return {}

    def representative_unit_id(self, *_args):
        return None

    def upsert_relations(self, relations):
        return len(list(relations))

    def mark_projection_complete(self, _release_id, *, expected_unit_count):
        self.expected_count = expected_unit_count
        actual = len(self.units)
        if actual != expected_unit_count:
            raise RuntimeError(
                f"expected {expected_unit_count} units, found {actual}"
            )
        return actual, 0

    def activate_release(self, _release_id):
        raise AssertionError("mismatched projection must not activate")


def test_indexer_propagates_source_expected_count_and_cannot_seal_partial_projection() -> None:
    target = _CountedTarget()

    with pytest.raises(RuntimeError, match="expected 2 units, found 1"):
        LegalEvidenceIndexer(_CountedSource(expected_count=2), target).publish(
            activate=True
        )

    assert target.expected_count == 2
    assert target.stage_projection_version == PROJECTION_VERSION
    assert PROJECTION_VERSION == "legal-evidence-projection-v6"
