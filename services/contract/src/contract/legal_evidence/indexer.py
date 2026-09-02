from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

from contract.application.idempotency import canonical_json
from contract.config import Settings
from contract.legal_evidence.embedding import OpenAICompatibleLegalEmbeddingProvider
from contract.legal_evidence.models import (
    LegalRelation,
    LegalRelationReviewItem,
    LegalRetrievalUnit,
)
from contract.legal_evidence.mysql_source import MySqlLegalSource
from contract.legal_evidence.postgres_repository import PostgresLegalEvidenceRepository
from contract.legal_evidence.projection import (
    LegalArticleAssembler,
    NamedInstrumentReference,
    RELATION_EXTRACTOR_VERSION,
    extract_internal_reference_spans,
    extract_named_instrument_references,
    unique_instrument_aliases,
)
from contract.persistence.postgres.migrate import run_migrations

PROJECTION_VERSION = "legal-evidence-projection-v5"
_KNOWN_RELATIONS = {
    "CITES",
    "BASED_ON",
    "IMPLEMENTS",
    "INTERPRETS",
    "AMENDS",
    "REPEALS",
    "REPLACES",
    "SUPPLEMENTS",
    "EXCEPTION_TO",
    "INTERNAL_REF",
}


class LegalEvidenceIndexer:
    """One-shot MySQL -> PostgreSQL projection publisher."""

    def __init__(
        self,
        source: MySqlLegalSource,
        target: PostgresLegalEvidenceRepository,
        *,
        embedding_provider: OpenAICompatibleLegalEmbeddingProvider | None = None,
        page_size: int = 1000,
        write_batch_size: int = 200,
        embedding_batch_size: int = 8,
        embedding_workers: int = 8,
    ) -> None:
        if (
            min(page_size, write_batch_size, embedding_batch_size, embedding_workers)
            <= 0
        ):
            raise ValueError("Indexer batch sizes must be positive")
        self.source = source
        self.target = target
        self.embedding_provider = embedding_provider
        self.page_size = page_size
        self.write_batch_size = write_batch_size
        self.embedding_batch_size = embedding_batch_size
        self.embedding_workers = embedding_workers

    @staticmethod
    def _embedding_text(item: LegalRetrievalUnit) -> str:
        return item.embedding_input

    def _embedding_model_version(self) -> str | None:
        if self.embedding_provider is None:
            return None
        registration = str(
            getattr(self.embedding_provider, "registration_id", "") or ""
        )
        model = str(getattr(self.embedding_provider, "model", "") or "")
        dimensions = getattr(self.embedding_provider, "dimensions", None)
        values = [registration, model]
        if dimensions is not None:
            values.append(str(dimensions))
        return ":".join(item for item in values if item) or type(
            self.embedding_provider
        ).__name__

    def _embed_batches(
        self, units: list[LegalRetrievalUnit]
    ) -> list[tuple[list[LegalRetrievalUnit], list[list[float]]]]:
        if self.embedding_provider is None or not units:
            return []
        batches = [
            units[start : start + self.embedding_batch_size]
            for start in range(0, len(units), self.embedding_batch_size)
        ]

        def embed(batch: list[LegalRetrievalUnit]) -> list[list[float]]:
            return self.embedding_provider.embed_documents(
                [self._embedding_text(item) for item in batch]
            )

        if len(batches) == 1 or self.embedding_workers == 1:
            vectors = [embed(batch) for batch in batches]
        else:
            with ThreadPoolExecutor(
                max_workers=min(self.embedding_workers, len(batches)),
                thread_name_prefix="legal-embedding",
            ) as executor:
                vectors = list(executor.map(embed, batches))
        return list(zip(batches, vectors, strict=True))

    def _expected_release_id(self, source_release_id: str | None) -> str:
        source_release = self.source.active_release()
        if source_release is None:
            raise RuntimeError("No active MySQL legal release exists")
        if source_release_id and source_release["release_id"] != source_release_id:
            raise RuntimeError("Requested source release is not active")
        identity = {
            "source_release_id": str(source_release["release_id"]),
            "source_manifest_sha256": str(source_release["manifest_sha256"]),
            "projection_version": PROJECTION_VERSION,
            "relation_extractor_version": RELATION_EXTRACTOR_VERSION,
            "embedding_profile_id": (
                self.embedding_provider.profile_id if self.embedding_provider else None
            ),
            "embedding_model_version": self._embedding_model_version(),
        }
        return "legal-index-" + hashlib.sha256(
            canonical_json(identity).encode("utf-8")
        ).hexdigest()[:32]

    def publish(
        self, *, source_release_id: str | None = None, activate: bool = False
    ) -> dict:
        expected_release_id = self._expected_release_id(source_release_id)
        lock_factory = getattr(self.target, "projection_lock", None)
        lock_context = (
            lock_factory(expected_release_id)
            if callable(lock_factory)
            else nullcontext()
        )
        with lock_context:
            return self._publish_locked(
                source_release_id=source_release_id,
                activate=activate,
                expected_release_id=expected_release_id,
            )

    def _publish_locked(
        self,
        *,
        source_release_id: str | None,
        activate: bool,
        expected_release_id: str,
    ) -> dict:
        source_release = self.source.active_release()
        if source_release is None:
            raise RuntimeError("No active MySQL legal release exists")
        if source_release_id and source_release["release_id"] != source_release_id:
            raise RuntimeError("Requested source release is not active")
        source_release_id = str(source_release["release_id"])
        expected_unit_count = self.source.retrieval_root_count(source_release_id)
        if expected_unit_count <= 0:
            raise RuntimeError(
                "The active MySQL legal release has no ARTICLE/PREAMBLE nodes"
            )
        embedding_model_version = self._embedding_model_version()
        previous_active_reader = getattr(self.target, "active_release", None)
        previous_active = (
            previous_active_reader() if callable(previous_active_reader) else None
        )
        reusable_embedding_release_id = (
            previous_active.release_id
            if previous_active is not None
            and self.embedding_provider is not None
            and previous_active.embedding_profile_id
            == self.embedding_provider.profile_id
            and previous_active.embedding_model_version == embedding_model_version
            else None
        )
        release_identity = {
            "source_release_id": source_release_id,
            "source_manifest_sha256": str(source_release["manifest_sha256"]),
            "projection_version": PROJECTION_VERSION,
            "relation_extractor_version": RELATION_EXTRACTOR_VERSION,
            "embedding_profile_id": (
                self.embedding_provider.profile_id if self.embedding_provider else None
            ),
            "embedding_model_version": embedding_model_version,
        }
        release_id = (
            "legal-index-"
            + hashlib.sha256(
                canonical_json(release_identity).encode("utf-8")
            ).hexdigest()[:32]
        )
        if release_id != expected_release_id:
            raise RuntimeError(
                "The active MySQL legal release changed before projection staging"
            )
        writable = self.target.stage_release(
            release_id=release_id,
            source_release_id=source_release_id,
            source_manifest_sha256=str(source_release["manifest_sha256"]),
            projection_version=PROJECTION_VERSION,
            relation_extractor_version=RELATION_EXTRACTOR_VERSION,
            embedding_profile_id=(
                self.embedding_provider.profile_id if self.embedding_provider else None
            ),
            embedding_model_version=embedding_model_version,
        )
        if not writable:
            if activate:
                self.target.activate_release(release_id)
            count_reader = getattr(self.target, "projection_counts", None)
            actual_units, actual_embeddings, actual_relations = (
                count_reader(release_id) if callable(count_reader) else (0, 0, 0)
            )
            return {
                "release_id": release_id,
                "source_release_id": source_release_id,
                "processed_units": actual_units,
                "projected_units": actual_units,
                "new_units": 0,
                "reused_units": actual_units,
                "embedded_units": actual_embeddings,
                "new_embeddings": 0,
                "reused_embeddings": actual_embeddings,
                "projected_relations": actual_relations,
                "relation_review_queue": 0,
                "actual_units": actual_units,
                "actual_embeddings": actual_embeddings,
                "actual_relations": actual_relations,
                "sealed_units": actual_units,
                "sealed_relations": actual_relations,
                "expected_units": expected_unit_count,
                "internal_references": 0,
                "named_references": 0,
                "named_references_linked": 0,
                "named_references_ambiguous": 0,
                "named_references_unmatched": 0,
                "activated": activate,
                "idempotent_existing_release": True,
            }

        assembler = LegalArticleAssembler()
        pending_units: list[LegalRetrievalUnit] = []
        projected = 0
        new_units = 0
        embedded = 0
        reused_units = 0
        reused_embeddings = 0
        copied_embeddings = 0
        started_at = time.monotonic()
        last_unit_report = 0
        resume_reader = getattr(self.target, "projection_resume_cursor", None)
        resume_cursor = resume_reader(release_id) if callable(resume_reader) else None
        resume_version_id = resume_cursor[0] if resume_cursor else ""
        base_existing_units = resume_cursor[2] if resume_cursor else 0
        base_existing_embeddings = resume_cursor[3] if resume_cursor else 0
        checkpoint_writer = getattr(self.target, "record_projection_checkpoint", None)

        def report_progress(*, phase: str, force: bool = False) -> None:
            nonlocal last_unit_report
            if not force and projected - last_unit_report < 10_000:
                return
            last_unit_report = projected
            print(
                json.dumps(
                    {
                        "event": "LEGAL_PROJECTION_PROGRESS",
                        "release_id": release_id,
                        "phase": phase,
                        "expected_units": expected_unit_count,
                        "processed_units": base_existing_units + new_units,
                        "embedded_units": (
                            base_existing_embeddings + embedded + copied_embeddings
                        ),
                        "new_units": new_units,
                        "new_embeddings": embedded,
                        "reused_units": reused_units,
                        "reused_embeddings": reused_embeddings,
                        "unit_percent": round(
                            100 * (base_existing_units + new_units) / expected_unit_count,
                            2,
                        ),
                        "elapsed_seconds": round(time.monotonic() - started_at, 1),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )

        def flush() -> None:
            nonlocal projected, new_units, embedded
            nonlocal reused_units, reused_embeddings, copied_embeddings
            if not pending_units:
                return
            batch = list(pending_units)
            pending_units.clear()
            projection_filter = getattr(self.target, "units_requiring_projection", None)
            units_to_write = (
                projection_filter(batch) if callable(projection_filter) else batch
            )
            self.target.upsert_units(units_to_write)
            projected += len(batch)
            new_units += len(units_to_write)
            reused_units += len(batch) - len(units_to_write)
            if self.embedding_provider is not None:
                embedding_copier = getattr(
                    self.target, "copy_compatible_embeddings", None
                )
                if reusable_embedding_release_id and callable(embedding_copier):
                    copied_embeddings += embedding_copier(
                        batch,
                        profile_id=self.embedding_provider.profile_id,
                        source_release_id=reusable_embedding_release_id,
                    )
                embedding_filter = getattr(
                    self.target, "units_requiring_embedding", None
                )
                units_to_embed = (
                    embedding_filter(
                        batch,
                        profile_id=self.embedding_provider.profile_id,
                    )
                    if callable(embedding_filter)
                    else batch
                )
                reused_embeddings += len(batch) - len(units_to_embed)
                embedding_batches = self._embed_batches(units_to_embed)
                if embedding_batches:
                    embedding_units = [
                        item
                        for batch_units, _batch_vectors in embedding_batches
                        for item in batch_units
                    ]
                    vectors = [
                        vector
                        for _batch_units, batch_vectors in embedding_batches
                        for vector in batch_vectors
                    ]
                    embedded += self.target.upsert_embeddings(
                        units=embedding_units,
                        vectors=vectors,
                        profile_id=self.embedding_provider.profile_id,
                        provider=self.embedding_provider.provider,
                        model=self.embedding_provider.model,
                    )
            if callable(checkpoint_writer):
                last_unit = batch[-1]
                checkpoint_writer(
                    source_release_id=source_release_id,
                    projection_release_id=release_id,
                    last_version_id=last_unit.version_id,
                    last_sequence=last_unit.sequence,
                    projected_unit_count=base_existing_units + new_units,
                    status="RUNNING",
                )
            report_progress(phase="UNITS_AND_EMBEDDINGS")

        node_iterator = (
            self.source.iter_nodes(
                source_release_id,
                page_size=self.page_size,
                start_version_id=resume_version_id,
            )
            if resume_cursor
            else self.source.iter_nodes(source_release_id, page_size=self.page_size)
        )
        for row in node_iterator:
            for unit in assembler.feed(row):
                unit = unit.model_copy(update={"release_id": release_id})
                pending_units.append(unit)
                if len(pending_units) >= self.write_batch_size:
                    flush()
        for unit in assembler.finish():
            unit = unit.model_copy(update={"release_id": release_id})
            pending_units.append(unit)
        flush()
        report_progress(phase="UNITS_AND_EMBEDDINGS_COMPLETE", force=True)

        relation_reset = getattr(self.target, "reset_staged_relations", None)
        if callable(relation_reset):
            relation_reset(release_id)

        relation_counter = getattr(self.source, "relation_count", None)
        authoritative_relation_count = (
            relation_counter(source_release_id)
            if callable(relation_counter)
            else 0
        )
        has_authoritative_relations = authoritative_relation_count > 0
        all_node_resolver = getattr(
            self.target, "all_unit_ids_by_source_nodes", None
        )
        authoritative_node_units = (
            all_node_resolver(release_id)
            if has_authoritative_relations and callable(all_node_resolver)
            else {}
        )

        # Older source releases can lack a deterministic relation table. Only
        # those releases use the projection-time extractor fallback. A modern
        # release's MySQL edges are authoritative and must not be duplicated by
        # a second extraction pass over assembled retrieval text.
        if has_authoritative_relations:
            unique_aliases: dict[str, str] = {}
            ambiguous_aliases: set[str] = set()
            representative_units: dict[str, str] = {}
        else:
            # Build an exact, unique title alias index. Ambiguous aliases are
            # never auto-linked because a wrong legal edge is worse than a
            # missing edge.
            unique_aliases, ambiguous_aliases = unique_instrument_aliases(
                self.source.iter_instruments(
                    source_release_id, page_size=self.page_size
                )
            )
            representative_units = self.target.representative_unit_ids(release_id)

        relations: list[LegalRelation] = []
        relation_reviews: list[LegalRelationReviewItem] = []
        relation_count = 0
        relation_review_count = 0
        internal_reference_count = 0
        named_reference_count = 0
        named_matched_count = 0
        named_ambiguous_count = 0
        named_unmatched_count = 0
        pending_named_article_links: list[
            tuple[str, str, str, str, NamedInstrumentReference]
        ] = []

        def flush_relations() -> None:
            nonlocal relation_count
            if not relations:
                return
            relation_count += self.target.upsert_relations(relations)
            relations.clear()

        def flush_relation_reviews() -> None:
            nonlocal relation_review_count
            if not relation_reviews:
                return
            writer = getattr(self.target, "upsert_relation_reviews", None)
            relation_review_count += (
                writer(relation_reviews) if callable(writer) else len(relation_reviews)
            )
            relation_reviews.clear()

        def append_relation_review(
            *,
            source_unit_id: str,
            source_node_id: str,
            reference: NamedInstrumentReference,
            review_reason: str,
        ) -> None:
            identity = (
                f"{release_id}|{source_unit_id}|{reference.normalized_title}|"
                f"{reference.target_article_no or ''}|{reference.relation_type}|"
                f"{reference.evidence_start}|{review_reason}"
            )
            relation_reviews.append(
                LegalRelationReviewItem(
                    review_id="legal-rel-review-"
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
                    release_id=release_id,
                    source_unit_id=source_unit_id,
                    source_node_id=source_node_id,
                    target_title=reference.title,
                    target_article_no=reference.target_article_no,
                    proposed_relation_type=reference.relation_type,
                    evidence_text=reference.evidence_text,
                    evidence_start=reference.evidence_start,
                    evidence_end=reference.evidence_end,
                    extractor_version=reference.extractor_version,
                    review_reason=review_reason,
                )
            )
            if len(relation_reviews) >= self.write_batch_size:
                flush_relation_reviews()

        def append_relation(
            *,
            source_unit_id: str,
            target_unit_id: str,
            relation_type: str,
            evidence_text: str,
            source_node_id: str | None = None,
            evidence_start: int | None = None,
            evidence_end: int | None = None,
            extractor_version: str = RELATION_EXTRACTOR_VERSION,
            confidence: float = 1,
            verification_status: str = "AUTO_VERIFIED",
        ) -> None:
            if source_unit_id == target_unit_id:
                return
            identity = (
                f"{release_id}|{source_unit_id}|{target_unit_id}|"
                f"{relation_type}|{evidence_text}"
            )
            relations.append(
                LegalRelation(
                    relation_id="legal-rel-"
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
                    release_id=release_id,
                    source_unit_id=source_unit_id,
                    target_unit_id=target_unit_id,
                    relation_type=relation_type,
                    evidence_text=evidence_text[:4000],
                    source_node_id=source_node_id,
                    evidence_start=evidence_start,
                    evidence_end=evidence_end,
                    extractor_version=extractor_version,
                    confidence=confidence,
                    verification_status=verification_status,
                )
            )
            if len(relations) >= self.write_batch_size:
                flush_relations()

        def process_version(units: list[LegalRetrievalUnit]) -> None:
            nonlocal internal_reference_count
            nonlocal named_reference_count, named_matched_count
            nonlocal named_ambiguous_count, named_unmatched_count
            article_units = {
                unit.article_no: unit.unit_id for unit in units if unit.article_no
            }
            for unit in units:
                for reference in extract_internal_reference_spans(unit.content):
                    target_unit_id = article_units.get(reference.article_no)
                    if not target_unit_id or target_unit_id == unit.unit_id:
                        continue
                    append_relation(
                        source_unit_id=unit.unit_id,
                        target_unit_id=target_unit_id,
                        relation_type="INTERNAL_REF",
                        evidence_text=reference.evidence_text,
                        source_node_id=unit.source_node_ids[0],
                        evidence_start=reference.evidence_start,
                        evidence_end=reference.evidence_end,
                        extractor_version=reference.extractor_version,
                    )
                    internal_reference_count += 1
                for reference in extract_named_instrument_references(unit.content):
                    named_reference_count += 1
                    target_instrument = unique_aliases.get(reference.normalized_title)
                    if target_instrument is None:
                        if reference.normalized_title in ambiguous_aliases:
                            named_ambiguous_count += 1
                            append_relation_review(
                                source_unit_id=unit.unit_id,
                                source_node_id=unit.source_node_ids[0],
                                reference=reference,
                                review_reason="AMBIGUOUS_TARGET",
                            )
                        else:
                            named_unmatched_count += 1
                            append_relation_review(
                                source_unit_id=unit.unit_id,
                                source_node_id=unit.source_node_ids[0],
                                reference=reference,
                                review_reason="UNMATCHED_TARGET",
                            )
                        continue
                    if reference.target_article_no:
                        pending_named_article_links.append(
                            (
                                unit.unit_id,
                                unit.source_node_ids[0],
                                unit.instrument_id,
                                target_instrument,
                                reference,
                            )
                        )
                        continue
                    else:
                        target_unit_id = representative_units.get(target_instrument)
                    if not target_unit_id or target_instrument == unit.instrument_id:
                        named_unmatched_count += 1
                        append_relation_review(
                            source_unit_id=unit.unit_id,
                            source_node_id=unit.source_node_ids[0],
                            reference=reference,
                            review_reason="UNMATCHED_TARGET",
                        )
                        continue
                    append_relation(
                        source_unit_id=unit.unit_id,
                        target_unit_id=target_unit_id,
                        relation_type=reference.relation_type,
                        evidence_text=reference.evidence_text,
                        source_node_id=unit.source_node_ids[0],
                        evidence_start=reference.evidence_start,
                        evidence_end=reference.evidence_end,
                        extractor_version=reference.extractor_version,
                        verification_status=reference.verification_status,
                    )
                    named_matched_count += 1

        # A second streaming pass avoids retaining tens of thousands of refs in
        # memory while allowing all target units to exist before edge creation.
        current_version_id: str | None = None
        version_units: list[LegalRetrievalUnit] = []
        runtime_units = (
            iter(())
            if has_authoritative_relations
            else self.target.iter_units(release_id, page_size=self.page_size)
        )
        for unit in runtime_units:
            if current_version_id is not None and unit.version_id != current_version_id:
                process_version(version_units)
                version_units.clear()
            current_version_id = unit.version_id
            version_units.append(unit)
        if version_units:
            process_version(version_units)

        article_targets = (
            self.target.unit_ids_by_instrument_articles(
                release_id,
                {
                    (target_instrument, reference.target_article_no)
                    for _, _, _, target_instrument, reference in pending_named_article_links
                    if reference.target_article_no
                },
            )
            if pending_named_article_links
            else {}
        )
        for (
            source_unit_id,
            source_node_id,
            source_instrument_id,
            target_instrument,
            reference,
        ) in pending_named_article_links:
            assert reference.target_article_no is not None
            target_unit_id = article_targets.get(
                (target_instrument, reference.target_article_no)
            )
            if not target_unit_id or target_instrument == source_instrument_id:
                named_unmatched_count += 1
                append_relation_review(
                    source_unit_id=source_unit_id,
                    source_node_id=source_node_id,
                    reference=reference,
                    review_reason="UNMATCHED_TARGET",
                )
                continue
            append_relation(
                source_unit_id=source_unit_id,
                target_unit_id=target_unit_id,
                relation_type=reference.relation_type,
                evidence_text=reference.evidence_text,
                source_node_id=source_node_id,
                evidence_start=reference.evidence_start,
                evidence_end=reference.evidence_end,
                extractor_version=reference.extractor_version,
                verification_status=reference.verification_status,
            )
            named_matched_count += 1

        def project_source_relations(rows: list[dict[str, object]]) -> None:
            node_ids = {
                str(node_id)
                for row in rows
                for node_id in (row.get("source_node_id"), row.get("target_node_id"))
                if node_id
            }
            if authoritative_node_units:
                node_units = {
                    node_id: authoritative_node_units.get(node_id)
                    for node_id in node_ids
                }
            else:
                node_resolver = getattr(
                    self.target, "unit_ids_by_source_nodes", None
                )
                node_units = (
                    node_resolver(release_id, node_ids)
                    if callable(node_resolver)
                    else {}
                )
            for row in rows:
                relation_type = str(row["relation_type"]).upper()
                if relation_type not in _KNOWN_RELATIONS:
                    continue
                source_node_id = (
                    str(row["source_node_id"]) if row.get("source_node_id") else None
                )
                target_node_id = (
                    str(row["target_node_id"]) if row.get("target_node_id") else None
                )
                source_unit_id = node_units.get(source_node_id or "")
                if not source_unit_id:
                    source_unit_id = self.target.representative_unit_id(
                        release_id,
                        str(row["source_instrument_key"]),
                        str(row["source_version_id"]),
                    )
                target_unit_id = node_units.get(target_node_id or "")
                if not target_unit_id:
                    target_unit_id = self.target.representative_unit_id(
                        release_id,
                        str(row["target_instrument_key"]),
                        (
                            str(row["target_version_id"])
                            if row.get("target_version_id")
                            else None
                        ),
                    )
                if (
                    not source_unit_id
                    or not target_unit_id
                    or source_unit_id == target_unit_id
                ):
                    continue
                raw_location = row.get("evidence_location_json") or {}
                if isinstance(raw_location, str):
                    try:
                        raw_location = json.loads(raw_location)
                    except (TypeError, ValueError):
                        raw_location = {}
                if not isinstance(raw_location, dict):
                    raw_location = {}
                append_relation(
                    source_unit_id=source_unit_id,
                    target_unit_id=target_unit_id,
                    relation_type=relation_type,
                    evidence_text=str(row.get("evidence_text") or "")[:4000],
                    source_node_id=source_node_id,
                    evidence_start=(
                        int(row["evidence_start"])
                        if row.get("evidence_start") is not None
                        else int(raw_location["start_byte"])
                        if raw_location.get("start_byte") is not None
                        else None
                    ),
                    evidence_end=(
                        int(row["evidence_end"])
                        if row.get("evidence_end") is not None
                        else int(raw_location["end_byte"])
                        if raw_location.get("end_byte") is not None
                        else None
                    ),
                    extractor_version=str(
                        row.get("extractor_version") or "source-release-explicit-v1"
                    ),
                    confidence=float(row["confidence"]),
                    verification_status=str(
                        row.get("verification_status") or "VERIFIED"
                    ),
                )

        source_relation_batch: list[dict[str, object]] = []
        for row in self.source.iter_relations(
            source_release_id, page_size=self.page_size
        ):
            source_relation_batch.append(row)
            if len(source_relation_batch) >= self.page_size:
                project_source_relations(source_relation_batch)
                source_relation_batch.clear()
        if source_relation_batch:
            project_source_relations(source_relation_batch)
        flush_relations()
        flush_relation_reviews()
        sealed_unit_count, sealed_relation_count = self.target.mark_projection_complete(
            release_id,
            expected_unit_count=expected_unit_count,
        )
        if activate:
            self.target.activate_release(release_id)
        count_reader = getattr(self.target, "projection_counts", None)
        actual_units, actual_embeddings, actual_relations = (
            count_reader(release_id)
            if callable(count_reader)
            else (
                sealed_unit_count,
                embedded + reused_embeddings,
                sealed_relation_count,
            )
        )
        if callable(checkpoint_writer):
            final_cursor = self.target.projection_resume_cursor(release_id)
            if final_cursor is not None:
                checkpoint_writer(
                    source_release_id=source_release_id,
                    projection_release_id=release_id,
                    last_version_id=final_cursor[0],
                    last_sequence=final_cursor[1],
                    projected_unit_count=actual_units,
                    projected_relation_count=actual_relations,
                    status="SUCCEEDED",
                )
        return {
            "release_id": release_id,
            "source_release_id": source_release_id,
            "processed_units": actual_units,
            "projected_units": actual_units,
            "new_units": new_units,
            "embedded_units": embedded + reused_embeddings,
            "new_embeddings": embedded,
            "reused_units": reused_units,
            "reused_embeddings": reused_embeddings,
            "projected_relations": relation_count,
            "relation_review_queue": relation_review_count,
            "actual_units": actual_units,
            "actual_embeddings": actual_embeddings,
            "actual_relations": actual_relations,
            "sealed_units": sealed_unit_count,
            "sealed_relations": sealed_relation_count,
            "expected_units": expected_unit_count,
            "internal_references": internal_reference_count,
            "named_references": named_reference_count,
            "named_references_linked": named_matched_count,
            "named_references_ambiguous": named_ambiguous_count,
            "named_references_unmatched": named_unmatched_count,
            "activated": activate,
            "idempotent_existing_release": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish legal evidence projection")
    parser.add_argument("--mysql-url", default=os.getenv("LEGAL_SOURCE_MYSQL_URL", ""))
    parser.add_argument(
        "--mysql-host", default=os.getenv("LEGAL_SOURCE_MYSQL_HOST", "")
    )
    parser.add_argument(
        "--mysql-port",
        type=int,
        default=int(os.getenv("LEGAL_SOURCE_MYSQL_PORT", "3306")),
    )
    parser.add_argument(
        "--mysql-user", default=os.getenv("LEGAL_SOURCE_MYSQL_USER", "")
    )
    parser.add_argument(
        "--mysql-password", default=os.getenv("LEGAL_SOURCE_MYSQL_PASSWORD", "")
    )
    parser.add_argument(
        "--mysql-database", default=os.getenv("LEGAL_SOURCE_MYSQL_DATABASE", "")
    )
    parser.add_argument(
        "--postgres-url", default=os.getenv("CONTRACT_DATABASE_URL", "")
    )
    parser.add_argument("--source-release-id")
    parser.add_argument(
        "--activate-release-id",
        help="Activate one already staged and validated projection release",
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--write-batch-size", type=int, default=200)
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=int(os.getenv("LEGAL_EMBEDDING_BATCH_SIZE", "8")),
    )
    parser.add_argument(
        "--embedding-workers",
        type=int,
        default=int(os.getenv("LEGAL_EMBEDDING_WORKERS", "8")),
    )
    parser.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--embedding-base-url", default=os.getenv("LEGAL_EMBEDDING_BASE_URL", "")
    )
    parser.add_argument(
        "--embedding-api-key", default=os.getenv("LEGAL_EMBEDDING_API_KEY", "")
    )
    parser.add_argument(
        "--embedding-registration-id",
        default=os.getenv("LEGAL_EMBEDDING_REGISTRATION_ID", ""),
    )
    parser.add_argument(
        "--embedding-model", default=os.getenv("LEGAL_EMBEDDING_MODEL", "")
    )
    parser.add_argument(
        "--embedding-max-attempts",
        type=int,
        default=int(os.getenv("LEGAL_EMBEDDING_MAX_ATTEMPTS", "4")),
    )
    parser.add_argument(
        "--embedding-retry-base-seconds",
        type=float,
        default=float(os.getenv("LEGAL_EMBEDDING_RETRY_BASE_SECONDS", "0.5")),
    )
    args = parser.parse_args()
    has_mysql_parts = all((args.mysql_host, args.mysql_user, args.mysql_database))
    if not args.postgres_url or (
        not args.activate_release_id and not args.mysql_url and not has_mysql_parts
    ):
        parser.error(
            "--postgres-url and either --mysql-url or "
            "--mysql-host/--mysql-user/--mysql-database are required"
        )
    settings = Settings(database_url=args.postgres_url, mock_mode=False)
    run_migrations(settings)
    target = PostgresLegalEvidenceRepository(settings)
    if args.activate_release_id:
        target.activate_release(args.activate_release_id)
        print(
            {
                "release_id": args.activate_release_id,
                "activated": True,
                "mode": "ACTIVATE_EXISTING",
            }
        )
        return
    embedding_provider = None
    if (
        args.embedding_base_url
        and args.embedding_registration_id
        and args.embedding_model
    ):
        embedding_provider = OpenAICompatibleLegalEmbeddingProvider(
            base_url=args.embedding_base_url,
            api_key=args.embedding_api_key,
            registration_id=args.embedding_registration_id,
            model=args.embedding_model,
            maximum_attempts=args.embedding_max_attempts,
            retry_base_seconds=args.embedding_retry_base_seconds,
        )
    result = LegalEvidenceIndexer(
        MySqlLegalSource(
            args.mysql_url or None,
            host=args.mysql_host or None,
            port=args.mysql_port,
            user=args.mysql_user or None,
            password=args.mysql_password,
            database=args.mysql_database or None,
        ),
        target,
        embedding_provider=embedding_provider,
        page_size=args.page_size,
        write_batch_size=args.write_batch_size,
        embedding_batch_size=args.embedding_batch_size,
        embedding_workers=args.embedding_workers,
    ).publish(source_release_id=args.source_release_id, activate=args.activate)
    print(result)


if __name__ == "__main__":
    main()
