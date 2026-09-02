from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from contract.application.idempotency import canonical_json
from contract.config import Settings
from contract.legal_evidence.models import (
    LegalEvidenceBundle,
    LegalEvidencePlanRequest,
    LegalEvidencePlanSnapshot,
    LegalEvidenceRelease,
    LegalEvidenceSnapshotConflict,
    LegalRelation,
    LegalRelationReviewItem,
    LegalRetrievalUnit,
    LegalSearchCandidate,
)
from contract.persistence.postgres.connections import open_contract_database_connection


def _vector_text(vector: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".17g") for value in vector) + "]"


class PostgresLegalEvidenceRepository:
    """PostgreSQL Adapter for the legal-evidence planner and indexer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _connect(self):
        return open_contract_database_connection(self.settings, row_factory=dict_row)

    @contextmanager
    def projection_lock(self, release_id: str):
        """Hold a session advisory lock for the complete projection publish."""

        lock_key = int.from_bytes(
            hashlib.sha256(release_id.encode("utf-8")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (lock_key,),
            ).fetchone()
            if row is None or not bool(row["acquired"]):
                raise RuntimeError(
                    "Another legal evidence projection is already publishing this release"
                )
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))

    def active_release(self) -> LegalEvidenceRelease | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT release_id, source_release_id, status, projection_version,
                       embedding_profile_id, relation_extractor_version,
                       embedding_model_version
                FROM legal_evidence_release
                WHERE status = 'ACTIVE'
                ORDER BY activated_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return LegalEvidenceRelease.model_validate(row) if row else None

    def exact_search(
        self,
        *,
        release_id: str,
        references: list[tuple[str, str | None]],
        jurisdiction: str | None,
        as_of_date: str,
        limit: int,
    ) -> list[LegalSearchCandidate]:
        """Resolve explicit ``《法规名》第X条`` references without fuzzy matching."""

        normalized = [
            (re.sub(r"[\s《》〈〉]", "", title).strip(), article_no)
            for title, article_no in references
            if re.sub(r"[\s《》〈〉]", "", title).strip()
        ]
        if not normalized:
            return []
        titles = [item[0] for item in normalized]
        articles = [item[1] for item in normalized]
        where, filter_params = self._applicability_sql(jurisdiction)
        params = [
            titles,
            articles,
            release_id,
            as_of_date,
            as_of_date,
            *filter_params,
            limit,
        ]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH requested(normalized_title, article_no) AS (
                  SELECT * FROM unnest(%s::text[], %s::text[])
                )
                SELECT DISTINCT ON (u.unit_id) u.*, 1.0::double precision AS score
                FROM requested r
                JOIN legal_evidence_unit u
                  ON regexp_replace(u.title, '[[:space:]《》〈〉]', '', 'g') = r.normalized_title
                 AND (r.article_no IS NULL OR u.article_no = r.article_no)
                WHERE u.release_id = %s
                  AND {" AND ".join(where)}
                ORDER BY u.unit_id, u.instrument_id, u.version_id, u.sequence
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [
            LegalSearchCandidate(
                unit=self._unit(row),
                score=1.0,
                channel="EXACT",
            )
            for row in rows
        ]

    @staticmethod
    def _applicability_sql(jurisdiction: str | None) -> tuple[list[str], list[Any]]:
        where = [
            "u.metadata_verification_status <> 'REJECTED'",
            "(u.metadata_verification_status <> 'VERIFIED' OR u.validity_status IS NULL OR u.validity_status NOT IN ('EXPIRED', 'REPEALED'))",
            "(u.metadata_verification_status <> 'VERIFIED' OR u.effective_from IS NULL OR u.effective_from <= %s::date)",
            "(u.metadata_verification_status <> 'VERIFIED' OR u.effective_to IS NULL OR u.effective_to >= %s::date)",
        ]
        params: list[Any] = []
        if jurisdiction:
            normalized_jurisdiction = jurisdiction.strip().upper()
            where.append(
                "(upper(u.jurisdiction) = %s OR "
                "%s LIKE upper(u.jurisdiction) || '-%%')"
            )
            params.extend([normalized_jurisdiction, normalized_jurisdiction])
        return where, params

    def keyword_search(
        self,
        *,
        release_id: str,
        query: str,
        jurisdiction: str | None,
        as_of_date: str,
        offset: int,
        limit: int,
    ) -> list[LegalSearchCandidate]:
        where, filter_params = self._applicability_sql(jurisdiction)
        params = [query, release_id, as_of_date, as_of_date, *filter_params, limit, offset]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH query_terms AS (
                  SELECT term
                  FROM unnest(
                    tsvector_to_array(to_tsvector('jiebacfg', %s))
                  ) AS term
                  WHERE char_length(term) >= 2
                  ORDER BY char_length(term) DESC, term
                  LIMIT 24
                ), query_value AS (
                  SELECT string_agg(quote_literal(term), ' | ')::tsquery AS value
                  FROM query_terms
                )
                SELECT u.*, ts_rank_cd(u.search_vector, query_value.value, 32) AS score
                FROM legal_evidence_unit u
                CROSS JOIN query_value
                WHERE u.release_id = %s
                  AND u.search_vector @@ query_value.value
                  AND {" AND ".join(where)}
                ORDER BY score DESC, u.instrument_id, u.version_id, u.sequence
                LIMIT %s OFFSET %s
                """,
                params,
            ).fetchall()
        return [
            LegalSearchCandidate(
                unit=self._unit(row),
                score=max(0.0, min(1.0, float(row["score"] or 0))),
                channel="KEYWORD",
            )
            for row in rows
        ]

    def vector_search(
        self,
        *,
        release_id: str,
        query_vector: list[float],
        embedding_profile_id: str,
        jurisdiction: str | None,
        as_of_date: str,
        offset: int,
        limit: int,
    ) -> list[LegalSearchCandidate]:
        where, filter_params = self._applicability_sql(jurisdiction)
        vector = _vector_text(query_vector)
        params = [vector, release_id, embedding_profile_id, as_of_date, as_of_date, *filter_params, vector, limit, offset]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT u.*, 1 - (e.embedding <=> %s::vector) AS score
                FROM legal_evidence_embedding e
                JOIN legal_evidence_unit u
                  ON u.release_id = e.release_id AND u.unit_id = e.unit_id
                WHERE u.release_id = %s
                  AND e.embedding_profile_id = %s
                  AND {" AND ".join(where)}
                ORDER BY e.embedding <=> %s::vector, u.unit_id
                LIMIT %s OFFSET %s
                """,
                params,
            ).fetchall()
        return [
            LegalSearchCandidate(
                unit=self._unit(row),
                score=max(0.0, min(1.0, float(row["score"] or 0))),
                channel="VECTOR",
            )
            for row in rows
        ]

    def relation_neighbors(
        self,
        *,
        release_id: str,
        unit_id: str,
        limit: int = 128,
    ) -> list[tuple[LegalRelation, LegalRetrievalUnit]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.relation_id, r.release_id, r.source_unit_id,
                       r.target_unit_id, r.relation_type, r.evidence_text,
                       r.source_node_id, r.evidence_start, r.evidence_end,
                       r.extractor_version, r.confidence, r.verification_status,
                       to_jsonb(u.*) AS target_unit
                FROM legal_evidence_relation r
                JOIN legal_evidence_unit u
                  ON u.release_id = r.release_id
                 AND u.unit_id = CASE
                   WHEN r.source_unit_id = %s THEN r.target_unit_id
                   ELSE r.source_unit_id
                 END
                WHERE r.release_id = %s
                  AND (r.source_unit_id = %s OR r.target_unit_id = %s)
                ORDER BY r.confidence DESC, r.relation_id
                LIMIT %s
                """,
                (unit_id, release_id, unit_id, unit_id, limit),
            ).fetchall()
        return [
            (
                LegalRelation.model_validate(
                    {key: value for key, value in row.items() if key != "target_unit"}
                ),
                self._unit(dict(row["target_unit"])),
            )
            for row in rows
        ]

    @staticmethod
    def _unit(row: dict[str, Any]) -> LegalRetrievalUnit:
        return LegalRetrievalUnit(
            unit_id=row["unit_id"],
            release_id=row["release_id"],
            instrument_id=row["instrument_id"],
            version_id=row["version_id"],
            source_node_ids=list(row["source_node_ids"]),
            title=row["title"],
            article_no=row.get("article_no"),
            heading_path=list(row.get("heading_path") or ()),
            content=row["content"],
            jurisdiction=row.get("jurisdiction"),
            authority_level=row.get("authority_level"),
            issuing_authority=row.get("issuing_authority"),
            effective_from=row.get("effective_from"),
            effective_to=row.get("effective_to"),
            validity_status=row.get("validity_status"),
            metadata_verification_status=row["metadata_verification_status"],
            official_source_url=row.get("official_source_url"),
            content_hash=row["content_hash"],
            sequence=row["sequence"],
        )

    def stage_release(
        self,
        *,
        release_id: str,
        source_release_id: str,
        source_manifest_sha256: str,
        projection_version: str,
        relation_extractor_version: str = "legal-relation-extractor-v1",
        embedding_profile_id: str | None = None,
        embedding_model_version: str | None = None,
    ) -> bool:
        """Create/resume a staged release without mutating a published one.

        Returns ``True`` when projection writes may continue. An identical
        ACTIVE/RETIRED release returns ``False`` for an idempotent no-op.
        Reusing an ID with different publication inputs is rejected.
        """
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO legal_evidence_release (
                  release_id, source_release_id, source_manifest_sha256,
                  projection_version, relation_extractor_version,
                  embedding_profile_id, embedding_model_version, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'STAGED')
                ON CONFLICT (release_id) DO NOTHING
                RETURNING release_id
                """,
                (
                    release_id,
                    source_release_id,
                    source_manifest_sha256,
                    projection_version,
                    relation_extractor_version,
                    embedding_profile_id,
                    embedding_model_version,
                ),
            ).fetchone()
            row = conn.execute(
                """
                SELECT source_release_id, source_manifest_sha256,
                       projection_version, relation_extractor_version,
                       embedding_profile_id, embedding_model_version, status
                FROM legal_evidence_release
                WHERE release_id = %s
                FOR UPDATE
                """,
                (release_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Legal evidence release disappeared while staging")
            expected = (
                source_release_id,
                source_manifest_sha256,
                projection_version,
                relation_extractor_version,
                embedding_profile_id,
                embedding_model_version,
            )
            actual = (
                str(row["source_release_id"]),
                str(row["source_manifest_sha256"]),
                str(row["projection_version"]),
                str(row.get("relation_extractor_version") or "legal-relation-extractor-v1"),
                row["embedding_profile_id"],
                row.get("embedding_model_version"),
            )
            if actual != expected:
                raise RuntimeError(
                    "Legal evidence release ID is already bound to different publication inputs"
                )
            if row["status"] == "STAGED":
                conn.execute(
                    """
                    UPDATE legal_evidence_release
                    SET projection_status = 'BUILDING',
                        projected_unit_count = 0,
                        projected_relation_count = 0,
                        projection_completed_at = NULL
                    WHERE release_id = %s
                    """,
                    (release_id,),
                )
            conn.commit()
        return inserted is not None or row["status"] == "STAGED"

    def mark_projection_complete(
        self,
        release_id: str,
        *,
        expected_unit_count: int | None = None,
    ) -> tuple[int, int]:
        """Seal one fully built STAGED projection before activation.

        Counts are computed in PostgreSQL rather than trusted from the worker's
        in-memory counters.  An interrupted publication therefore remains in
        ``BUILDING`` and cannot be activated accidentally.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.status, r.projection_status, r.embedding_profile_id,
                       r.projected_unit_count, r.projected_relation_count,
                       (SELECT count(*) FROM legal_evidence_unit u
                        WHERE u.release_id = r.release_id) AS unit_count,
                       (SELECT count(*) FROM legal_evidence_relation e
                        WHERE e.release_id = r.release_id) AS relation_count,
                       (SELECT count(*) FROM legal_evidence_embedding e
                        WHERE e.release_id = r.release_id
                          AND e.embedding_profile_id = r.embedding_profile_id) AS embedding_count,
                       (SELECT count(*)
                        FROM legal_evidence_embedding e
                        JOIN legal_evidence_unit u
                          ON u.release_id = e.release_id AND u.unit_id = e.unit_id
                        WHERE e.release_id = r.release_id
                          AND e.embedding_profile_id = r.embedding_profile_id
                          AND (e.content_hash <> u.content_hash
                               OR e.embedding_input_hash <> u.embedding_input_hash)
                       ) AS invalid_embedding_count
                FROM legal_evidence_release r
                WHERE r.release_id = %s
                FOR UPDATE
                """,
                (release_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Legal evidence release does not exist")
            if row["status"] != "STAGED":
                raise RuntimeError("Only a STAGED legal evidence release can be sealed")
            if row["projection_status"] not in {"BUILDING", "READY"}:
                raise RuntimeError("Only a BUILDING legal evidence projection can be sealed")
            unit_count = int(row["unit_count"] or 0)
            relation_count = int(row["relation_count"] or 0)
            if unit_count <= 0:
                raise RuntimeError("Refusing to seal an empty legal evidence release")
            if expected_unit_count is not None and unit_count != expected_unit_count:
                raise RuntimeError(
                    "Refusing to seal an incomplete legal evidence release: "
                    f"expected {expected_unit_count} units, found {unit_count}"
                )
            if row["embedding_profile_id"] and int(row["embedding_count"] or 0) != unit_count:
                raise RuntimeError(
                    "Refusing to seal a partially embedded legal evidence release"
                )
            if int(row.get("invalid_embedding_count") or 0) != 0:
                raise RuntimeError(
                    "Refusing to seal a legal evidence release with drifted embeddings"
                )
            if row["projection_status"] == "READY":
                if (
                    int(row["projected_unit_count"] or 0) != unit_count
                    or int(row["projected_relation_count"] or 0) != relation_count
                ):
                    raise RuntimeError(
                        "Refusing to reuse a sealed legal evidence release whose counts drifted"
                    )
                return unit_count, relation_count
            conn.execute(
                """
                UPDATE legal_evidence_release
                SET projection_status = 'READY',
                    projected_unit_count = %s,
                    projected_relation_count = %s,
                    projection_completed_at = now()
                WHERE release_id = %s
                """,
                (unit_count, relation_count, release_id),
            )
            conn.commit()
        return unit_count, relation_count

    def upsert_units(self, units: Iterable[LegalRetrievalUnit]) -> int:
        rows = list(units)
        if not rows:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                INSERT INTO legal_evidence_unit (
                  release_id, unit_id, instrument_id, version_id,
                  source_node_ids, title, article_no, heading_path, content,
                  jurisdiction, authority_level, issuing_authority,
                  effective_from, effective_to, validity_status,
                  metadata_verification_status, official_source_url,
                  content_hash, projection_hash, embedding_input_hash, sequence
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (release_id, unit_id) DO NOTHING
                    """,
                    [
                        (
                            item.release_id,
                            item.unit_id,
                            item.instrument_id,
                            item.version_id,
                            item.source_node_ids,
                            item.title,
                            item.article_no,
                            item.heading_path,
                            item.content,
                            item.jurisdiction,
                            item.authority_level,
                            item.issuing_authority,
                            item.effective_from,
                            item.effective_to,
                            item.validity_status,
                            item.metadata_verification_status,
                            item.official_source_url,
                            item.content_hash,
                            item.projection_hash,
                            item.embedding_input_hash,
                            item.sequence,
                        )
                        for item in rows
                    ],
                )
            conn.commit()
        return len(rows)

    def units_requiring_projection(
        self, units: Iterable[LegalRetrievalUnit]
    ) -> list[LegalRetrievalUnit]:
        """Return units absent from an interrupted immutable projection."""
        rows = list(units)
        if not rows:
            return []
        release_ids = {item.release_id for item in rows}
        if len(release_ids) != 1:
            raise ValueError("Projection unit batch must belong to one release")
        release_id = next(iter(release_ids))
        with self._connect() as conn:
            existing = {
                str(row["unit_id"]): str(row["projection_hash"])
                for row in conn.execute(
                    """
                    SELECT unit_id, projection_hash
                    FROM legal_evidence_unit
                    WHERE release_id = %s AND unit_id = ANY(%s)
                    """,
                    (release_id, [item.unit_id for item in rows]),
                ).fetchall()
            }
        drifted = [
            item.unit_id
            for item in rows
            if item.unit_id in existing
            and existing[item.unit_id] != item.projection_hash
        ]
        if drifted:
            raise RuntimeError("Existing projection unit identity drifted")
        return [item for item in rows if item.unit_id not in existing]

    def units_requiring_embedding(
        self,
        units: Iterable[LegalRetrievalUnit],
        *,
        profile_id: str,
    ) -> list[LegalRetrievalUnit]:
        """Return units without an exact embedding for this immutable profile."""
        rows = list(units)
        if not rows:
            return []
        release_ids = {item.release_id for item in rows}
        if len(release_ids) != 1:
            raise ValueError("Embedding unit batch must belong to one release")
        release_id = next(iter(release_ids))
        with self._connect() as conn:
            existing = {
                str(row["unit_id"]): str(row["embedding_input_hash"])
                for row in conn.execute(
                    """
                    SELECT unit_id, embedding_input_hash
                    FROM legal_evidence_embedding
                    WHERE release_id = %s
                      AND embedding_profile_id = %s
                      AND unit_id = ANY(%s)
                    """,
                    (release_id, profile_id, [item.unit_id for item in rows]),
                ).fetchall()
            }
        drifted = [
            item.unit_id
            for item in rows
            if item.unit_id in existing
            and existing[item.unit_id] != item.embedding_input_hash
        ]
        if drifted:
            raise RuntimeError("Existing embedding input hash drifted")
        return [item for item in rows if item.unit_id not in existing]

    def copy_compatible_embeddings(
        self,
        units: Iterable[LegalRetrievalUnit],
        *,
        profile_id: str,
        source_release_id: str,
    ) -> int:
        """Copy immutable vectors from a prior compatible projection.

        Projection algorithm changes can require a new release while leaving
        the exact embedding input and model profile unchanged. Hash checks make
        that reuse deterministic and avoid an unnecessary external model call.
        """
        rows = list(units)
        if not rows:
            return 0
        target_release_ids = {item.release_id for item in rows}
        if len(target_release_ids) != 1:
            raise ValueError("Embedding unit batch must belong to one release")
        target_release_id = next(iter(target_release_ids))
        if target_release_id == source_release_id:
            return 0
        with self._connect() as conn:
            copied = conn.execute(
                """
                WITH requested(unit_id, content_hash, embedding_input_hash) AS (
                  SELECT * FROM unnest(%s::text[], %s::text[], %s::text[])
                )
                INSERT INTO legal_evidence_embedding (
                  release_id, unit_id, embedding_profile_id, provider,
                  model, dimensions, embedding, content_hash,
                  embedding_input_hash
                )
                SELECT %s, e.unit_id, e.embedding_profile_id, e.provider,
                       e.model, e.dimensions, e.embedding, e.content_hash,
                       e.embedding_input_hash
                FROM requested r
                JOIN legal_evidence_embedding e
                  ON e.release_id = %s
                 AND e.unit_id = r.unit_id
                 AND e.embedding_profile_id = %s
                 AND e.content_hash = r.content_hash
                 AND e.embedding_input_hash = r.embedding_input_hash
                ON CONFLICT (release_id, unit_id, embedding_profile_id) DO NOTHING
                RETURNING unit_id
                """,
                (
                    [item.unit_id for item in rows],
                    [item.content_hash for item in rows],
                    [item.embedding_input_hash for item in rows],
                    target_release_id,
                    source_release_id,
                    profile_id,
                ),
            ).fetchall()
            conn.commit()
        return len(copied)

    def upsert_embeddings(
        self,
        *,
        units: list[LegalRetrievalUnit],
        vectors: list[list[float]],
        profile_id: str,
        provider: str,
        model: str,
    ) -> int:
        if len(units) != len(vectors):
            raise ValueError("Embedding count must match unit count")
        if not units:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                INSERT INTO legal_evidence_embedding (
                  release_id, unit_id, embedding_profile_id, provider,
                  model, dimensions, embedding, content_hash,
                  embedding_input_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
                ON CONFLICT (release_id, unit_id, embedding_profile_id) DO NOTHING
                    """,
                    [
                        (
                            unit.release_id,
                            unit.unit_id,
                            profile_id,
                            provider,
                            model,
                            len(vector),
                            _vector_text(vector),
                            unit.content_hash,
                            unit.embedding_input_hash,
                        )
                        for unit, vector in zip(units, vectors, strict=True)
                    ],
                )
            conn.commit()
        return len(units)

    def representative_unit_id(
        self, release_id: str, instrument_id: str, version_id: str | None = None
    ) -> str | None:
        with self._connect() as conn:
            if version_id:
                row = conn.execute(
                    """
                    SELECT unit_id FROM legal_evidence_unit
                    WHERE release_id = %s AND instrument_id = %s AND version_id = %s
                    ORDER BY CASE WHEN article_no IS NULL THEN 1 ELSE 0 END, sequence
                    LIMIT 1
                    """,
                    (release_id, instrument_id, version_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    WITH unique_version AS (
                      SELECT min(version_id) AS version_id
                      FROM legal_evidence_unit
                      WHERE release_id = %s AND instrument_id = %s
                      HAVING count(DISTINCT version_id) = 1
                    )
                    SELECT u.unit_id
                    FROM legal_evidence_unit u
                    JOIN unique_version v ON v.version_id = u.version_id
                    WHERE u.release_id = %s AND u.instrument_id = %s
                    ORDER BY CASE WHEN u.article_no IS NULL THEN 1 ELSE 0 END, u.sequence
                    LIMIT 1
                    """,
                    (release_id, instrument_id, release_id, instrument_id),
                ).fetchone()
        return str(row["unit_id"]) if row else None

    def representative_unit_ids(self, release_id: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH unique_versions AS (
                  SELECT instrument_id, min(version_id) AS version_id
                  FROM legal_evidence_unit
                  WHERE release_id = %s
                  GROUP BY instrument_id
                  HAVING count(DISTINCT version_id) = 1
                )
                SELECT DISTINCT ON (u.instrument_id) u.instrument_id, u.unit_id
                FROM legal_evidence_unit u
                JOIN unique_versions v
                  ON v.instrument_id = u.instrument_id AND v.version_id = u.version_id
                WHERE u.release_id = %s
                ORDER BY u.instrument_id,
                         CASE WHEN u.article_no IS NULL THEN 1 ELSE 0 END,
                         u.sequence, u.unit_id
                """,
                (release_id, release_id),
            ).fetchall()
        return {str(row["instrument_id"]): str(row["unit_id"]) for row in rows}

    def unit_ids_by_source_nodes(
        self,
        release_id: str,
        node_ids: set[str],
        *,
        batch_size: int = 2000,
    ) -> dict[str, str | None]:
        """Resolve authoritative source nodes to their containing retrieval unit.

        A node should occur in exactly one assembled unit. Any accidental
        ambiguity fails closed so a legal edge is never attached to an
        arbitrary article merely because it belongs to the same version.
        """
        ordered = sorted(node_id for node_id in node_ids if node_id)
        resolved: dict[str, str | None] = {}
        for start in range(0, len(ordered), batch_size):
            batch = ordered[start : start + batch_size]
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT source_node_id,
                           array_agg(u.unit_id ORDER BY u.sequence, u.unit_id) AS unit_ids
                    FROM legal_evidence_unit u
                    CROSS JOIN LATERAL unnest(u.source_node_ids) AS source_node_id
                    WHERE u.release_id = %s
                      AND source_node_id = ANY(%s::text[])
                    GROUP BY source_node_id
                    """,
                    (release_id, batch),
                ).fetchall()
            for row in rows:
                unit_ids = list(row["unit_ids"] or ())
                resolved[str(row["source_node_id"])] = (
                    str(unit_ids[0]) if len(unit_ids) == 1 else None
                )
        return resolved

    def all_unit_ids_by_source_nodes(
        self,
        release_id: str,
    ) -> dict[str, str | None]:
        """Build one streaming node-to-unit map for authoritative edge import.

        Loading the map once is substantially cheaper than rescanning hundreds
        of thousands of units for every source relation page. Ambiguous nodes
        remain mapped to ``None`` and therefore fail closed.
        """
        resolved: dict[str, str | None] = {}
        with self._connect() as conn:
            with conn.cursor(name="legal_source_node_unit_map") as cursor:
                cursor.execute(
                    """
                    SELECT unit_id, source_node_ids
                    FROM legal_evidence_unit
                    WHERE release_id = %s
                    ORDER BY version_id, sequence
                    """,
                    (release_id,),
                )
                for row in cursor:
                    unit_id = str(row["unit_id"])
                    for raw_node_id in row["source_node_ids"]:
                        node_id = str(raw_node_id)
                        if node_id not in resolved:
                            resolved[node_id] = unit_id
                        elif resolved[node_id] != unit_id:
                            resolved[node_id] = None
        return resolved

    def iter_units(
        self, release_id: str, *, page_size: int = 1000
    ) -> Iterable[LegalRetrievalUnit]:
        last_version_id = ""
        last_sequence = -1
        while True:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM legal_evidence_unit
                    WHERE release_id = %s
                      AND (version_id > %s OR (version_id = %s AND sequence > %s))
                    ORDER BY version_id, sequence
                    LIMIT %s
                    """,
                    (
                        release_id,
                        last_version_id,
                        last_version_id,
                        last_sequence,
                        page_size,
                    ),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield self._unit(row)
            last_version_id = str(rows[-1]["version_id"])
            last_sequence = int(rows[-1]["sequence"])

    def unit_id_by_article(
        self, release_id: str, version_id: str, article_no: str
    ) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT unit_id FROM legal_evidence_unit
                WHERE release_id = %s AND version_id = %s AND article_no = %s
                ORDER BY sequence LIMIT 1
                """,
                (release_id, version_id, article_no),
            ).fetchone()
        return str(row["unit_id"]) if row else None

    def unit_id_by_instrument_article(
        self,
        release_id: str,
        instrument_id: str,
        article_no: str,
    ) -> str | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH unique_version AS (
                  SELECT min(version_id) AS version_id
                  FROM legal_evidence_unit
                  WHERE release_id = %s AND instrument_id = %s
                  HAVING count(DISTINCT version_id) = 1
                )
                SELECT u.unit_id
                FROM legal_evidence_unit u
                JOIN unique_version v ON v.version_id = u.version_id
                WHERE u.release_id = %s
                  AND u.instrument_id = %s
                  AND u.article_no = %s
                ORDER BY u.sequence
                LIMIT 2
                """,
                (release_id, instrument_id, release_id, instrument_id, article_no),
            ).fetchall()
        # A title-only citation does not identify a legal version. Even when
        # only one version happens to contain the requested article number,
        # multiple projected versions remain ambiguous and must fail closed.
        return str(rows[0]["unit_id"]) if len(rows) == 1 else None

    def unit_ids_by_instrument_articles(
        self,
        release_id: str,
        keys: set[tuple[str, str]],
        *,
        batch_size: int = 1000,
    ) -> dict[tuple[str, str], str | None]:
        """Resolve exact article targets in bounded bulk queries.

        Ambiguous matches intentionally map to ``None``. This replaces the
        former one-connection-per-reference path during a large publication.
        """
        ordered = sorted(keys)
        resolved: dict[tuple[str, str], str | None] = {}
        for start in range(0, len(ordered), batch_size):
            batch = ordered[start : start + batch_size]
            instruments = [item[0] for item in batch]
            articles = [item[1] for item in batch]
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    WITH requested(instrument_id, article_no) AS (
                      SELECT * FROM unnest(%s::text[], %s::text[])
                    ), requested_instruments AS (
                      SELECT DISTINCT instrument_id FROM requested
                    ), unique_versions AS (
                      SELECT u.instrument_id, min(u.version_id) AS version_id
                      FROM legal_evidence_unit u
                      JOIN requested_instruments r
                        ON r.instrument_id = u.instrument_id
                      WHERE u.release_id = %s
                      GROUP BY u.instrument_id
                      HAVING count(DISTINCT u.version_id) = 1
                    )
                    SELECT r.instrument_id, r.article_no,
                           array_agg(u.unit_id ORDER BY u.sequence)
                             FILTER (WHERE u.unit_id IS NOT NULL) AS unit_ids
                    FROM requested r
                    LEFT JOIN unique_versions v
                      ON v.instrument_id = r.instrument_id
                    LEFT JOIN legal_evidence_unit u
                      ON u.release_id = %s
                     AND u.instrument_id = r.instrument_id
                     AND u.version_id = v.version_id
                     AND u.article_no = r.article_no
                    GROUP BY r.instrument_id, r.article_no
                    """,
                    (instruments, articles, release_id, release_id),
                ).fetchall()
            for row in rows:
                unit_ids = [value for value in (row["unit_ids"] or []) if value]
                key = (str(row["instrument_id"]), str(row["article_no"]))
                resolved[key] = str(unit_ids[0]) if len(unit_ids) == 1 else None
        return resolved

    def upsert_relations(self, relations: Iterable[LegalRelation]) -> int:
        rows = list(relations)
        if not rows:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                INSERT INTO legal_evidence_relation (
                  release_id, relation_id, source_unit_id, target_unit_id,
                  relation_type, evidence_text, source_node_id,
                  evidence_start, evidence_end, extractor_version,
                  confidence, verification_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (release_id, relation_id) DO NOTHING
                    """,
                    [
                        (
                            item.release_id,
                            item.relation_id,
                            item.source_unit_id,
                            item.target_unit_id,
                            item.relation_type,
                            item.evidence_text,
                            item.source_node_id,
                            item.evidence_start,
                            item.evidence_end,
                            item.extractor_version,
                            item.confidence,
                            item.verification_status,
                        )
                        for item in rows
                    ],
                )
            conn.commit()
        return len(rows)

    def upsert_relation_reviews(
        self, reviews: Iterable[LegalRelationReviewItem]
    ) -> int:
        rows = list(reviews)
        if not rows:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO legal_evidence_relation_review_queue (
                      release_id, review_id, source_unit_id, source_node_id,
                      target_title, target_article_no, proposed_relation_type,
                      evidence_text, evidence_start, evidence_end,
                      extractor_version, review_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (release_id, review_id) DO NOTHING
                    """,
                    [
                        (
                            item.release_id,
                            item.review_id,
                            item.source_unit_id,
                            item.source_node_id,
                            item.target_title,
                            item.target_article_no,
                            item.proposed_relation_type,
                            item.evidence_text,
                            item.evidence_start,
                            item.evidence_end,
                            item.extractor_version,
                            item.review_reason,
                        )
                        for item in rows
                    ],
                )
            conn.commit()
        return len(rows)

    def reset_staged_relations(self, release_id: str) -> None:
        """Clear an interrupted relation phase before deterministic rebuild."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, projection_status
                FROM legal_evidence_release
                WHERE release_id = %s
                FOR UPDATE
                """,
                (release_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Legal evidence release does not exist")
            if row["status"] != "STAGED" or row["projection_status"] != "BUILDING":
                raise RuntimeError(
                    "Relations can only be rebuilt for a STAGED/BUILDING projection"
                )
            conn.execute(
                "DELETE FROM legal_evidence_relation WHERE release_id = %s",
                (release_id,),
            )
            conn.execute(
                "DELETE FROM legal_evidence_relation_review_queue WHERE release_id = %s",
                (release_id,),
            )
            conn.commit()

    def projection_counts(self, release_id: str) -> tuple[int, int, int]:
        """Return authoritative unit, embedding, and relation counts."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM legal_evidence_unit u
                   WHERE u.release_id = r.release_id) AS unit_count,
                  (SELECT count(*) FROM legal_evidence_embedding e
                   WHERE e.release_id = r.release_id
                     AND e.embedding_profile_id = r.embedding_profile_id) AS embedding_count,
                  (SELECT count(*) FROM legal_evidence_relation x
                   WHERE x.release_id = r.release_id) AS relation_count
                FROM legal_evidence_release r
                WHERE r.release_id = %s
                """,
                (release_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Legal evidence release does not exist")
        return (
            int(row["unit_count"] or 0),
            int(row["embedding_count"] or 0),
            int(row["relation_count"] or 0),
        )

    def projection_resume_cursor(self, release_id: str) -> tuple[str, int, int, int] | None:
        """Return the last complete checkpoint and current counts for safe replay.

        A flush persists units before embeddings.  A process can therefore die
        with newer unit rows that were never embedded.  The maximum unit row is
        not a safe resume boundary; prefer the checkpoint written only after
        both writes completed, and replay everything after that boundary.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                WITH totals AS (
                  SELECT
                    (SELECT count(*) FROM legal_evidence_unit
                     WHERE release_id = %s) AS unit_count,
                    (SELECT count(*) FROM legal_evidence_embedding
                     WHERE release_id = %s) AS embedding_count
                )
                SELECT c.last_version_id AS version_id,
                       c.last_sequence AS sequence,
                       totals.unit_count, totals.embedding_count,
                       missing.version_id AS missing_version_id,
                       missing.sequence AS missing_sequence
                FROM legal_evidence_projection_checkpoint c CROSS JOIN totals
                LEFT JOIN LATERAL (
                  SELECT u.version_id, u.sequence
                  FROM legal_evidence_unit u
                  JOIN legal_evidence_release r ON r.release_id = u.release_id
                  LEFT JOIN legal_evidence_embedding e
                    ON e.release_id = u.release_id
                   AND e.unit_id = u.unit_id
                   AND e.embedding_profile_id = r.embedding_profile_id
                  WHERE u.release_id = %s
                    AND r.embedding_profile_id IS NOT NULL
                    AND e.unit_id IS NULL
                  ORDER BY u.version_id, u.sequence
                  LIMIT 1
                ) missing ON true
                WHERE c.projection_release_id = %s
                """,
                (release_id, release_id, release_id, release_id),
            ).fetchone()
            if row is None:
                # Compatibility for projections created before durable
                # checkpoints existed. Replaying the latest complete version
                # still lets the hash filters avoid duplicate writes.
                row = conn.execute(
                    """
                    WITH totals AS (
                      SELECT
                        (SELECT count(*) FROM legal_evidence_unit
                         WHERE release_id = %s) AS unit_count,
                        (SELECT count(*) FROM legal_evidence_embedding
                         WHERE release_id = %s) AS embedding_count
                    )
                    SELECT u.version_id, u.sequence, totals.unit_count,
                           totals.embedding_count
                    FROM legal_evidence_unit u CROSS JOIN totals
                    WHERE u.release_id = %s
                    ORDER BY u.version_id DESC, u.sequence DESC
                    LIMIT 1
                    """,
                    (release_id, release_id, release_id),
                ).fetchone()
        if row is None:
            return None
        version_id = str(row["version_id"])
        sequence = int(row["sequence"])
        missing_version_id = row.get("missing_version_id")
        if missing_version_id is not None and str(missing_version_id) < version_id:
            # A pre-checkpoint process may have used the historical max-row
            # cursor and advanced beyond an incomplete unit/embedding flush.
            # Rewind to the first objectively incomplete version; hash-based
            # filters make replay idempotent.
            version_id = str(missing_version_id)
            sequence = int(row.get("missing_sequence") or 0)
        return (
            version_id,
            sequence,
            int(row["unit_count"]),
            int(row["embedding_count"]),
        )

    def record_projection_checkpoint(
        self,
        *,
        source_release_id: str,
        projection_release_id: str,
        last_version_id: str,
        last_sequence: int,
        projected_unit_count: int,
        projected_relation_count: int = 0,
        status: str = "RUNNING",
        error_code: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO legal_evidence_projection_checkpoint (
                  source_release_id, projection_release_id, last_version_id,
                  last_sequence, projected_unit_count, projected_relation_count,
                  status, error_code, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (projection_release_id) DO UPDATE SET
                  source_release_id = EXCLUDED.source_release_id,
                  last_version_id = EXCLUDED.last_version_id,
                  last_sequence = EXCLUDED.last_sequence,
                  projected_unit_count = EXCLUDED.projected_unit_count,
                  projected_relation_count = EXCLUDED.projected_relation_count,
                  status = EXCLUDED.status,
                  error_code = EXCLUDED.error_code,
                  updated_at = now()
                """,
                (
                    source_release_id,
                    projection_release_id,
                    last_version_id,
                    last_sequence,
                    projected_unit_count,
                    projected_relation_count,
                    status,
                    error_code,
                ),
            )
            conn.commit()

    def activate_release(self, release_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.status, r.projection_status, r.embedding_profile_id,
                       r.projected_unit_count, r.projected_relation_count,
                       (SELECT count(*) FROM legal_evidence_unit u
                        WHERE u.release_id = r.release_id) AS unit_count,
                       (SELECT count(*) FROM legal_evidence_relation e
                        WHERE e.release_id = r.release_id) AS relation_count,
                       (SELECT count(*) FROM legal_evidence_embedding e
                        WHERE e.release_id = r.release_id
                          AND e.embedding_profile_id = r.embedding_profile_id) AS embedding_count,
                       (SELECT count(*)
                        FROM legal_evidence_embedding e
                        JOIN legal_evidence_unit u
                          ON u.release_id = e.release_id AND u.unit_id = e.unit_id
                        WHERE e.release_id = r.release_id
                          AND e.embedding_profile_id = r.embedding_profile_id
                          AND (e.content_hash <> u.content_hash
                               OR e.embedding_input_hash <> u.embedding_input_hash)
                       ) AS invalid_embedding_count
                FROM legal_evidence_release r
                WHERE r.release_id = %s
                FOR UPDATE
                """,
                (release_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Legal evidence release does not exist")
            if row["status"] not in {"STAGED", "ACTIVE", "RETIRED"}:
                raise RuntimeError("Legal evidence release has an invalid lifecycle status")
            if row["projection_status"] != "READY":
                raise RuntimeError("Refusing to activate a projection that is not READY")
            unit_count = int(row["unit_count"] or 0)
            relation_count = int(row["relation_count"] or 0)
            if unit_count <= 0:
                raise RuntimeError("Refusing to activate an empty legal evidence release")
            if (
                int(row["projected_unit_count"] or 0) != unit_count
                or int(row["projected_relation_count"] or 0) != relation_count
            ):
                raise RuntimeError(
                    "Refusing to activate a legal evidence release whose sealed counts drifted"
                )
            if row["embedding_profile_id"] and int(row["embedding_count"] or 0) != int(
                unit_count
            ):
                raise RuntimeError(
                    "Refusing to activate a partially embedded legal evidence release"
                )
            if int(row.get("invalid_embedding_count") or 0) != 0:
                raise RuntimeError(
                    "Refusing to activate a legal evidence release with drifted embeddings"
                )
            if row["status"] == "ACTIVE":
                # Activation of the current release is an idempotent no-op. In
                # particular, do not rewrite activated_at on every retry.
                return
            conn.execute(
                "UPDATE legal_evidence_release SET status = 'RETIRED' WHERE status = 'ACTIVE' AND release_id <> %s",
                (release_id,),
            )
            conn.execute(
                """
                UPDATE legal_evidence_release r SET
                  status = 'ACTIVE', activated_at = now()
                WHERE r.release_id = %s
                """,
                (release_id,),
            )
            conn.commit()

    def save_plan_snapshot(
        self,
        *,
        request: LegalEvidencePlanRequest,
        bundle: LegalEvidenceBundle,
        attempt_no: int,
    ) -> LegalEvidencePlanSnapshot:
        """Freeze a success without ever replacing the first decision.

        Concurrent planners may finish with different outcomes.  The row that
        wins the insert is authoritative; every caller reads and returns that
        same decision instead of using its own losing in-memory result.
        """
        request_hash = request.stable_hash
        bundle_payload = bundle.model_dump(mode="json")
        key = (request.review_id, request.generation_id, attempt_no)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO legal_evidence_plan_snapshot (
                  review_id, generation_id, attempt_no, release_id,
                  request_hash, bundle_hash, bundle_json, status, degraded
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (review_id, generation_id, attempt_no) DO NOTHING
                """,
                (
                    *key,
                    bundle.release_id,
                    request_hash,
                    bundle.bundle_hash,
                    Jsonb(bundle_payload),
                    bundle.status,
                    bundle.status == "DEGRADED" or bool(bundle.degraded_channels),
                ),
            )
            row = self._select_plan_snapshot(conn, key)
            conn.commit()
        if row is None:
            raise RuntimeError("Legal evidence snapshot conflict could not be read")
        return self._snapshot_from_row(row, expected_request_hash=request_hash)

    def load_plan_snapshot(
        self,
        *,
        review_id: str,
        generation_id: str,
        attempt_no: int,
        expected_request_hash: str,
    ) -> LegalEvidencePlanSnapshot | None:
        with self._connect() as conn:
            row = self._select_plan_snapshot(
                conn,
                (review_id, generation_id, attempt_no),
            )
        if row is None:
            return None
        return self._snapshot_from_row(
            row,
            expected_request_hash=expected_request_hash,
        )

    def save_failed_plan_snapshot(
        self,
        *,
        request: LegalEvidencePlanRequest,
        attempt_no: int,
        error_type: str,
    ) -> LegalEvidencePlanSnapshot:
        request_hash = request.stable_hash
        normalized_error_type = error_type[:160] or "UnknownPlannerError"
        failure_payload = {
            "status": "PLANNER_FAILED",
            "error_type": normalized_error_type,
        }
        bundle_hash = "sha256:" + hashlib.sha256(
            canonical_json(failure_payload).encode("utf-8")
        ).hexdigest()
        key = (request.review_id, request.generation_id, attempt_no)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO legal_evidence_plan_snapshot (
                  review_id, generation_id, attempt_no, release_id,
                  request_hash, bundle_hash, bundle_json, status, degraded
                ) VALUES (%s, %s, %s, NULL, %s, %s, %s, 'PLANNER_FAILED', true)
                ON CONFLICT (review_id, generation_id, attempt_no) DO NOTHING
                """,
                (*key, request_hash, bundle_hash, Jsonb(failure_payload)),
            )
            row = self._select_plan_snapshot(conn, key)
            conn.commit()
        if row is None:
            raise RuntimeError("Legal evidence snapshot conflict could not be read")
        return self._snapshot_from_row(row, expected_request_hash=request_hash)

    @staticmethod
    def _select_plan_snapshot(conn, key: tuple[str, str, int]):
        return conn.execute(
            """
            SELECT request_hash, bundle_hash, bundle_json, status
            FROM legal_evidence_plan_snapshot
            WHERE review_id = %s AND generation_id = %s AND attempt_no = %s
            """,
            key,
        ).fetchone()

    @staticmethod
    def _snapshot_from_row(
        row,
        *,
        expected_request_hash: str,
    ) -> LegalEvidencePlanSnapshot:
        stored_request_hash = str(row["request_hash"])
        if stored_request_hash != expected_request_hash:
            raise LegalEvidenceSnapshotConflict(
                "Legal evidence snapshot request hash conflict"
            )
        payload = row["bundle_json"]
        if isinstance(payload, Jsonb):
            payload = payload.obj
        status = str(row["status"])
        if status == "PLANNER_FAILED":
            error_type = (
                payload.get("error_type") if isinstance(payload, dict) else None
            )
            return LegalEvidencePlanSnapshot(
                request_hash=stored_request_hash,
                bundle_hash=str(row["bundle_hash"]),
                status="PLANNER_FAILED",
                error_type=error_type or "UnknownPlannerError",
            )
        bundle = LegalEvidenceBundle.model_validate(payload)
        return LegalEvidencePlanSnapshot(
            request_hash=stored_request_hash,
            bundle_hash=str(row["bundle_hash"]),
            status=status,
            bundle=bundle,
        )
