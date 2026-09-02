from __future__ import annotations

import hashlib
import os
import uuid

import psycopg
import pytest
from contract.config import Settings
from contract.legal_evidence.indexer import PROJECTION_VERSION
from contract.legal_evidence.models import LegalRelation, LegalRetrievalUnit
from contract.legal_evidence.postgres_repository import (
    PostgresLegalEvidenceRepository,
)
from contract.persistence.postgres.migrate import run_migrations

DATABASE_URL = os.getenv("CONTRACT_TEST_DATABASE_URL", "")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unit(
    *,
    release_id: str,
    unit_id: str,
    sequence: int,
    title: str,
) -> LegalRetrievalUnit:
    content = f"{title}第{sequence + 1}条的测试正文。"
    return LegalRetrievalUnit(
        unit_id=unit_id,
        release_id=release_id,
        instrument_id="instrument-integration",
        version_id="version-integration",
        source_node_ids=[f"node-{sequence}"],
        title=title,
        article_no=f"第{sequence + 1}条",
        heading_path=["第一章"],
        content=content,
        jurisdiction="CN",
        authority_level="LAW",
        issuing_authority="集成测试机关",
        validity_status="ACTIVE",
        metadata_verification_status="VERIFIED",
        official_source_url="https://example.invalid/legal-test",
        content_hash=_sha256(content),
        sequence=sequence,
    )


def _count(sql: str, params: tuple[object, ...]) -> int:
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="CONTRACT_TEST_DATABASE_URL is not configured",
)
def test_projection_resume_hashes_relation_rebuild_and_release_lock(
    request: pytest.FixtureRequest,
) -> None:
    """Exercise the publication invariants against a real PostgreSQL server."""

    settings = Settings(
        database_url=DATABASE_URL,
        database_application_name="legal-projection-integration",
    )
    run_migrations(settings)
    repository = PostgresLegalEvidenceRepository(settings)
    previous_active = repository.active_release()

    suffix = uuid.uuid4().hex
    release_id = f"legal-index-integration-{suffix}"
    profile_id = f"embedding-profile-integration-{suffix}"

    def restore_previous_active_release() -> None:
        if previous_active is not None and previous_active.release_id != release_id:
            repository.activate_release(previous_active.release_id)

    # Publishing is intentionally immutable, so the integration release remains
    # as a RETIRED audit record. Always restore the release that was active when
    # the test started instead of leaving test data selected for real queries.
    request.addfinalizer(restore_previous_active_release)

    with (
        repository.projection_lock(release_id),
        pytest.raises(RuntimeError, match="already publishing"),
        repository.projection_lock(release_id),
    ):
        pass
    with repository.projection_lock(release_id):
        pass

    assert repository.stage_release(
        release_id=release_id,
        source_release_id=f"source-integration-{suffix}",
        source_manifest_sha256="a" * 64,
        projection_version=PROJECTION_VERSION,
        embedding_profile_id=profile_id,
    )

    first = _unit(
        release_id=release_id,
        unit_id=f"unit-integration-{suffix}-1",
        sequence=0,
        title="集成测试法规",
    )
    second = _unit(
        release_id=release_id,
        unit_id=f"unit-integration-{suffix}-2",
        sequence=1,
        title="集成测试法规",
    )
    units = [first, second]

    assert repository.units_requiring_projection(units) == units
    assert repository.upsert_units(units) == 2
    assert repository.units_requiring_projection(units) == []
    assert repository.unit_ids_by_source_nodes(
        release_id,
        {first.source_node_ids[0], second.source_node_ids[0], "missing-node"},
    ) == {
        first.source_node_ids[0]: first.unit_id,
        second.source_node_ids[0]: second.unit_id,
    }
    repository.record_projection_checkpoint(
        source_release_id=f"source-integration-{suffix}",
        projection_release_id=release_id,
        last_version_id=second.version_id,
        last_sequence=second.sequence,
        projected_unit_count=2,
        status="RUNNING",
    )
    assert repository.projection_resume_cursor(release_id) == (
        second.version_id,
        second.sequence,
        2,
        0,
    )

    for drifted in (
        first.model_copy(update={"title": "更名后的法规"}),
        first.model_copy(update={"sequence": 99}),
        first.model_copy(update={"source_node_ids": ["different-node"]}),
        first.model_copy(update={"jurisdiction": "CN-11"}),
    ):
        with pytest.raises(RuntimeError, match="identity drifted"):
            repository.units_requiring_projection([drifted])

    assert repository.upsert_embeddings(
        units=[first],
        vectors=[[0.001] * 1024],
        profile_id=profile_id,
        provider="integration",
        model="integration-1024",
    ) == 1
    assert repository.units_requiring_embedding(
        units, profile_id=profile_id
    ) == [second]
    assert repository.upsert_embeddings(
        units=[second],
        vectors=[[0.002] * 1024],
        profile_id=profile_id,
        provider="integration",
        model="integration-1024",
    ) == 1
    assert repository.units_requiring_embedding(units, profile_id=profile_id) == []

    with pytest.raises(RuntimeError, match="input hash drifted"):
        repository.units_requiring_embedding(
            [first.model_copy(update={"title": "向量输入已改变"})],
            profile_id=profile_id,
        )

    old_relation = LegalRelation(
        relation_id=f"legal-rel-integration-{suffix}-old",
        release_id=release_id,
        source_unit_id=first.unit_id,
        target_unit_id=second.unit_id,
        relation_type="CITES",
        evidence_text="旧关系应在恢复重建时消失",
        confidence=1.0,
        verification_status="AUTO_VERIFIED",
    )
    assert repository.upsert_relations([old_relation]) == 1
    repository.reset_staged_relations(release_id)
    assert _count(
        "SELECT count(*) FROM legal_evidence_relation WHERE release_id = %s",
        (release_id,),
    ) == 0

    new_relation = old_relation.model_copy(
        update={
            "relation_id": f"legal-rel-integration-{suffix}-new",
            "relation_type": "INTERNAL_REF",
            "evidence_text": "确定性重建的新关系",
        }
    )
    assert repository.upsert_relations([new_relation]) == 1

    assert repository.projection_counts(release_id) == (2, 2, 1)
    assert repository.mark_projection_complete(
        release_id,
        expected_unit_count=2,
    ) == (2, 1)
    repository.activate_release(release_id)

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            SELECT status, projection_status, projected_unit_count,
                   projected_relation_count
            FROM legal_evidence_release
            WHERE release_id = %s
            """,
            (release_id,),
        ).fetchone()
        migration = conn.execute(
            """
            SELECT count(*) FROM contract_schema_migration
            WHERE version = '008_legal_evidence_resume_hashes'
            """
        ).fetchone()
        checkpoint_migration = conn.execute(
            """
            SELECT count(*) FROM contract_schema_migration
            WHERE version = '010_legal_evidence_resumable_checkpoint'
            """
        ).fetchone()
        checkpoint = conn.execute(
            """
            SELECT source_release_id, projection_release_id, status
            FROM legal_evidence_projection_checkpoint
            WHERE projection_release_id = %s
            """,
            (release_id,),
        ).fetchone()
        old_edges = conn.execute(
            """
            SELECT count(*) FROM legal_evidence_relation
            WHERE release_id = %s AND relation_id = %s
            """,
            (release_id, old_relation.relation_id),
        ).fetchone()

    assert row == ("ACTIVE", "READY", 2, 1)
    assert migration == (1,)
    assert checkpoint_migration == (1,)
    assert checkpoint == (
        f"source-integration-{suffix}",
        release_id,
        "RUNNING",
    )
    assert old_edges == (0,)
