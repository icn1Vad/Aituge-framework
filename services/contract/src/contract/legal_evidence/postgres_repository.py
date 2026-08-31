from __future__ import annotations

import hashlib
from collections.abc import Iterable
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

    def active_release(self) -> LegalEvidenceRelease | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT release_id, source_release_id, status, projection_version,
                       embedding_profile_id
                FROM legal_evidence_release
                WHERE status = 'ACTIVE'
                ORDER BY activated_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return LegalEvidenceRelease.model_validate(row) if row else None

    @staticmethod
    def _applicability_sql(jurisdiction: str | None) -> tuple[list[str], list[Any]]:
        where = [
            "u.metadata_verification_status <> 'REJECTED'",
            "(u.metadata_verification_status <> 'VERIFIED' OR u.validity_status IS NULL OR u.validity_status = 'ACTIVE')",
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
                  SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', %s))) AS term
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
                       r.confidence, r.verification_status,
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
        embedding_profile_id: str | None,
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
                  projection_version, embedding_profile_id, status
                ) VALUES (%s, %s, %s, %s, %s, 'STAGED')
                ON CONFLICT (release_id) DO NOTHING
                RETURNING release_id
                """,
                (
                    release_id,
                    source_release_id,
                    source_manifest_sha256,
                    projection_version,
                    embedding_profile_id,
                ),
            ).fetchone()
            row = conn.execute(
                """
                SELECT source_release_id, source_manifest_sha256,
                       projection_version, embedding_profile_id, status
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
                embedding_profile_id,
            )
            actual = (
                str(row["source_release_id"]),
                str(row["source_manifest_sha256"]),
                str(row["projection_version"]),
                row["embedding_profile_id"],
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
                          AND e.embedding_profile_id = r.embedding_profile_id) AS embedding_count
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
            conn.executemany(
                """
                INSERT INTO legal_evidence_unit (
                  release_id, unit_id, instrument_id, version_id,
                  source_node_ids, title, article_no, heading_path, content,
                  jurisdiction, authority_level, issuing_authority,
                  effective_from, effective_to, validity_status,
                  metadata_verification_status, official_source_url,
                  content_hash, sequence
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (release_id, unit_id) DO UPDATE SET
                  source_node_ids = EXCLUDED.source_node_ids,
                  title = EXCLUDED.title,
                  article_no = EXCLUDED.article_no,
                  heading_path = EXCLUDED.heading_path,
                  content = EXCLUDED.content,
                  jurisdiction = EXCLUDED.jurisdiction,
                  authority_level = EXCLUDED.authority_level,
                  issuing_authority = EXCLUDED.issuing_authority,
                  effective_from = EXCLUDED.effective_from,
                  effective_to = EXCLUDED.effective_to,
                  validity_status = EXCLUDED.validity_status,
                  metadata_verification_status = EXCLUDED.metadata_verification_status,
                  official_source_url = EXCLUDED.official_source_url,
                  content_hash = EXCLUDED.content_hash,
                  sequence = EXCLUDED.sequence
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
                        item.sequence,
                    )
                    for item in rows
                ],
            )
            conn.commit()
        return len(rows)

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
            conn.executemany(
                """
                INSERT INTO legal_evidence_embedding (
                  release_id, unit_id, embedding_profile_id, provider,
                  model, dimensions, embedding, content_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT (release_id, unit_id, embedding_profile_id) DO UPDATE SET
                  provider = EXCLUDED.provider,
                  model = EXCLUDED.model,
                  dimensions = EXCLUDED.dimensions,
                  embedding = EXCLUDED.embedding,
                  content_hash = EXCLUDED.content_hash,
                  created_at = now()
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
            conn.executemany(
                """
                INSERT INTO legal_evidence_relation (
                  release_id, relation_id, source_unit_id, target_unit_id,
                  relation_type, evidence_text, confidence, verification_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (release_id, relation_id) DO UPDATE SET
                  source_unit_id = EXCLUDED.source_unit_id,
                  target_unit_id = EXCLUDED.target_unit_id,
                  relation_type = EXCLUDED.relation_type,
                  evidence_text = EXCLUDED.evidence_text,
                  confidence = EXCLUDED.confidence,
                  verification_status = EXCLUDED.verification_status
                """,
                [
                    (
                        item.release_id,
                        item.relation_id,
                        item.source_unit_id,
                        item.target_unit_id,
                        item.relation_type,
                        item.evidence_text,
                        item.confidence,
                        item.verification_status,
                    )
                    for item in rows
                ],
            )
            conn.commit()
        return len(rows)

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
                          AND e.embedding_profile_id = r.embedding_profile_id) AS embedding_count
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
