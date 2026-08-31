from __future__ import annotations

import argparse
import hashlib
import os

from contract.application.idempotency import canonical_json
from contract.config import Settings
from contract.legal_evidence.embedding import OpenAICompatibleLegalEmbeddingProvider
from contract.legal_evidence.models import LegalRelation, LegalRetrievalUnit
from contract.legal_evidence.mysql_source import MySqlLegalSource
from contract.legal_evidence.postgres_repository import PostgresLegalEvidenceRepository
from contract.legal_evidence.projection import (
    LegalArticleAssembler,
    NamedInstrumentReference,
    extract_internal_references,
    extract_named_instrument_references,
    unique_instrument_aliases,
)
from contract.persistence.postgres.migrate import run_migrations

PROJECTION_VERSION = "legal-evidence-projection-v2"
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
        embedding_batch_size: int = 32,
    ) -> None:
        if min(page_size, write_batch_size, embedding_batch_size) <= 0:
            raise ValueError("Indexer batch sizes must be positive")
        self.source = source
        self.target = target
        self.embedding_provider = embedding_provider
        self.page_size = page_size
        self.write_batch_size = write_batch_size
        self.embedding_batch_size = embedding_batch_size

    def publish(self, *, source_release_id: str | None = None, activate: bool = False) -> dict:
        source_release = self.source.active_release()
        if source_release is None:
            raise RuntimeError("No active MySQL legal release exists")
        if source_release_id and source_release["release_id"] != source_release_id:
            raise RuntimeError("Requested source release is not active")
        source_release_id = str(source_release["release_id"])
        expected_unit_count = self.source.retrieval_root_count(source_release_id)
        if expected_unit_count <= 0:
            raise RuntimeError("The active MySQL legal release has no ARTICLE/PREAMBLE nodes")
        release_identity = {
            "source_release_id": source_release_id,
            "source_manifest_sha256": str(source_release["manifest_sha256"]),
            "projection_version": PROJECTION_VERSION,
            "embedding_profile_id": (
                self.embedding_provider.profile_id if self.embedding_provider else None
            ),
        }
        release_id = "legal-index-" + hashlib.sha256(
            canonical_json(release_identity).encode("utf-8")
        ).hexdigest()[:32]
        writable = self.target.stage_release(
            release_id=release_id,
            source_release_id=source_release_id,
            source_manifest_sha256=str(source_release["manifest_sha256"]),
            projection_version=PROJECTION_VERSION,
            embedding_profile_id=(
                self.embedding_provider.profile_id if self.embedding_provider else None
            ),
        )
        if not writable:
            if activate:
                self.target.activate_release(release_id)
            return {
                "release_id": release_id,
                "source_release_id": source_release_id,
                "projected_units": 0,
                "embedded_units": 0,
                "projected_relations": 0,
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
        embedded = 0

        def flush() -> None:
            nonlocal projected, embedded
            if not pending_units:
                return
            self.target.upsert_units(pending_units)
            projected += len(pending_units)
            if self.embedding_provider is not None:
                for start in range(0, len(pending_units), self.embedding_batch_size):
                    embedding_units = pending_units[
                        start : start + self.embedding_batch_size
                    ]
                    vectors = self.embedding_provider.embed_documents(
                        [
                            "\n".join(
                                part
                                for part in (
                                    item.title,
                                    item.article_no or "",
                                    item.content,
                                )
                                if part
                            )
                            for item in embedding_units
                        ]
                    )
                    embedded += self.target.upsert_embeddings(
                        units=embedding_units,
                        vectors=vectors,
                        profile_id=self.embedding_provider.profile_id,
                        provider=self.embedding_provider.provider,
                        model=self.embedding_provider.model,
                    )
            pending_units.clear()

        for row in self.source.iter_nodes(source_release_id, page_size=self.page_size):
            for unit in assembler.feed(row):
                unit = unit.model_copy(update={"release_id": release_id})
                pending_units.append(unit)
                if len(pending_units) >= self.write_batch_size:
                    flush()
        for unit in assembler.finish():
            unit = unit.model_copy(update={"release_id": release_id})
            pending_units.append(unit)
        flush()

        # Build an exact, unique title alias index. Ambiguous aliases are never
        # auto-linked because a wrong legal edge is worse than a missing edge.
        unique_aliases, ambiguous_aliases = unique_instrument_aliases(
            self.source.iter_instruments(
                source_release_id, page_size=self.page_size
            )
        )
        representative_units = self.target.representative_unit_ids(release_id)

        relations: list[LegalRelation] = []
        relation_count = 0
        internal_reference_count = 0
        named_reference_count = 0
        named_matched_count = 0
        named_ambiguous_count = 0
        named_unmatched_count = 0
        pending_named_article_links: list[
            tuple[str, str, str, NamedInstrumentReference]
        ] = []

        def flush_relations() -> None:
            nonlocal relation_count
            if not relations:
                return
            relation_count += self.target.upsert_relations(relations)
            relations.clear()

        def append_relation(
            *,
            source_unit_id: str,
            target_unit_id: str,
            relation_type: str,
            evidence_text: str,
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
                for article_no in extract_internal_references(unit.content):
                    target_unit_id = article_units.get(article_no)
                    if not target_unit_id or target_unit_id == unit.unit_id:
                        continue
                    marker = unit.content.find(article_no)
                    start = max(0, marker - 64) if marker >= 0 else 0
                    end = min(len(unit.content), marker + len(article_no) + 64)
                    append_relation(
                        source_unit_id=unit.unit_id,
                        target_unit_id=target_unit_id,
                        relation_type="INTERNAL_REF",
                        evidence_text=unit.content[start:end],
                    )
                    internal_reference_count += 1
                for reference in extract_named_instrument_references(unit.content):
                    named_reference_count += 1
                    target_instrument = unique_aliases.get(reference.normalized_title)
                    if target_instrument is None:
                        if reference.normalized_title in ambiguous_aliases:
                            named_ambiguous_count += 1
                        else:
                            named_unmatched_count += 1
                        continue
                    if reference.target_article_no:
                        pending_named_article_links.append(
                            (
                                unit.unit_id,
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
                        continue
                    append_relation(
                        source_unit_id=unit.unit_id,
                        target_unit_id=target_unit_id,
                        relation_type=reference.relation_type,
                        evidence_text=reference.evidence_text,
                        verification_status=reference.verification_status,
                    )
                    named_matched_count += 1

        # A second streaming pass avoids retaining tens of thousands of refs in
        # memory while allowing all target units to exist before edge creation.
        current_version_id: str | None = None
        version_units: list[LegalRetrievalUnit] = []
        for unit in self.target.iter_units(release_id, page_size=self.page_size):
            if current_version_id is not None and unit.version_id != current_version_id:
                process_version(version_units)
                version_units.clear()
            current_version_id = unit.version_id
            version_units.append(unit)
        if version_units:
            process_version(version_units)

        article_targets = self.target.unit_ids_by_instrument_articles(
            release_id,
            {
                (target_instrument, reference.target_article_no)
                for _, _, target_instrument, reference in pending_named_article_links
                if reference.target_article_no
            },
        )
        for (
            source_unit_id,
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
                continue
            append_relation(
                source_unit_id=source_unit_id,
                target_unit_id=target_unit_id,
                relation_type=reference.relation_type,
                evidence_text=reference.evidence_text,
                verification_status=reference.verification_status,
            )
            named_matched_count += 1

        for row in self.source.iter_relations(source_release_id, page_size=self.page_size):
            relation_type = str(row["relation_type"]).upper()
            if relation_type not in _KNOWN_RELATIONS:
                continue
            source_unit_id = self.target.representative_unit_id(
                release_id,
                str(row["source_instrument_key"]),
                str(row["source_version_id"]),
            )
            target_unit_id = self.target.representative_unit_id(
                release_id,
                str(row["target_instrument_key"]),
                str(row["target_version_id"]) if row.get("target_version_id") else None,
            )
            if not source_unit_id or not target_unit_id or source_unit_id == target_unit_id:
                continue
            append_relation(
                source_unit_id=source_unit_id,
                target_unit_id=target_unit_id,
                relation_type=relation_type,
                evidence_text=str(row.get("evidence_text") or "")[:4000],
                confidence=float(row["confidence"]),
                verification_status="VERIFIED",
            )
        flush_relations()
        sealed_unit_count, sealed_relation_count = self.target.mark_projection_complete(
            release_id,
            expected_unit_count=expected_unit_count,
        )
        if activate:
            self.target.activate_release(release_id)
        return {
            "release_id": release_id,
            "source_release_id": source_release_id,
            "projected_units": projected,
            "embedded_units": embedded,
            "projected_relations": relation_count,
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
    parser.add_argument("--mysql-host", default=os.getenv("LEGAL_SOURCE_MYSQL_HOST", ""))
    parser.add_argument(
        "--mysql-port",
        type=int,
        default=int(os.getenv("LEGAL_SOURCE_MYSQL_PORT", "3306")),
    )
    parser.add_argument("--mysql-user", default=os.getenv("LEGAL_SOURCE_MYSQL_USER", ""))
    parser.add_argument(
        "--mysql-password", default=os.getenv("LEGAL_SOURCE_MYSQL_PASSWORD", "")
    )
    parser.add_argument(
        "--mysql-database", default=os.getenv("LEGAL_SOURCE_MYSQL_DATABASE", "")
    )
    parser.add_argument("--postgres-url", default=os.getenv("CONTRACT_DATABASE_URL", ""))
    parser.add_argument("--source-release-id")
    parser.add_argument(
        "--activate-release-id",
        help="Activate one already staged and validated projection release",
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--write-batch-size", type=int, default=200)
    parser.add_argument("--embedding-batch-size", type=int, default=32)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--embedding-base-url", default=os.getenv("LEGAL_EMBEDDING_BASE_URL", ""))
    parser.add_argument("--embedding-api-key", default=os.getenv("LEGAL_EMBEDDING_API_KEY", ""))
    parser.add_argument(
        "--embedding-registration-id",
        default=os.getenv("LEGAL_EMBEDDING_REGISTRATION_ID", ""),
    )
    parser.add_argument("--embedding-model", default=os.getenv("LEGAL_EMBEDDING_MODEL", ""))
    args = parser.parse_args()
    has_mysql_parts = all((args.mysql_host, args.mysql_user, args.mysql_database))
    if not args.postgres_url or (
        not args.activate_release_id
        and not args.mysql_url
        and not has_mysql_parts
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
    if args.embedding_base_url and args.embedding_registration_id and args.embedding_model:
        embedding_provider = OpenAICompatibleLegalEmbeddingProvider(
            base_url=args.embedding_base_url,
            api_key=args.embedding_api_key,
            registration_id=args.embedding_registration_id,
            model=args.embedding_model,
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
    ).publish(source_release_id=args.source_release_id, activate=args.activate)
    print(result)


if __name__ == "__main__":
    main()
