from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from proof.application.conflict_retrieval.title_normalizer import normalize_policy_title
from proof.application.similarity import (
    DEFAULT_VERSION,
    SimilarityThresholds,
    normalize_similarity_text,
    normalized_text_hash,
    similarity_report,
)
from proof.application.structure import (
    STRUCTURE_ENGINE_VERSION,
    extract_policy_structure,
)
from proof.config import Settings
from proof.domain import DocumentBlock, ParsedDocument, StructureExtractionResult
from proof.domain.policy_grouping import infer_policy_category
from proof.errors import ProofError
from proof.infrastructure.parsers import (
    PARSER_VERSION,
    SUPPORTED_EXTENSIONS,
    parse_document_bytes,
)
from proof.infrastructure.postgres.repository import ProofRepository
from proof.tenant import tenant_storage_key

logger = logging.getLogger(__name__)


class PolicySourceParser(Protocol):
    version: str
    supported_extensions: frozenset[str]

    def parse(self, content: bytes, filename: str) -> ParsedDocument: ...


class PolicyClauseExtractor(Protocol):
    version: str

    def extract(self, blocks: list[DocumentBlock]) -> StructureExtractionResult: ...


class NativePolicySourceParser:
    version = PARSER_VERSION
    supported_extensions = frozenset(SUPPORTED_EXTENSIONS)

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        return parse_document_bytes(content, filename)


class PresetPolicyStructureExtractor:
    version = STRUCTURE_ENGINE_VERSION

    def extract(self, blocks: list[DocumentBlock]) -> StructureExtractionResult:
        return extract_policy_structure(blocks)


class PolicyIngestionPipeline:
    def __init__(
        self,
        settings: Settings,
        repository: ProofRepository,
        *,
        parser: PolicySourceParser | None = None,
        extractor: PolicyClauseExtractor | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.parser = parser or NativePolicySourceParser()
        self.extractor = extractor or PresetPolicyStructureExtractor()
        self.storage_root = settings.resolved_storage_root()
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def ingest_policy(
        self,
        *,
        content: bytes,
        filename: str,
        title: str = "",
        version: str = DEFAULT_VERSION,
        level_code: str | None = None,
        category_code: str = "auto",
    ) -> dict[str, Any]:
        original_name, suffix = self._validate_input(content, filename)
        content_hash = hashlib.sha256(content).hexdigest()
        run_id = uuid.uuid4().hex
        try:
            self.repository.create_ingestion_run(
                run_id=run_id,
                content_hash=content_hash,
                original_name=original_name,
                parser_version=self.parser.version,
                clause_profile="auto",
            )
        except Exception as exc:
            logger.exception("Unable to create policy ingestion run %s", run_id)
            raise ProofError(
                "ingestion_failed",
                "Policy ingestion failed.",
                status_code=500,
            ) from exc

        stage = "deduplicate"
        target: Path | None = None
        created_file = False
        try:
            existing = self.repository.get_by_content_hash(content_hash)
            if existing:
                document_id = existing["document"]["id"]
                counts = self.repository.get_document_counts(document_id)
                structure_warnings = existing["document"].get("structure_diagnostics", {}).get("warnings", [])
                self.repository.complete_ingestion_run(
                    run_id,
                    policy_id=existing["policy"]["id"],
                    document_id=document_id,
                    reused=True,
                    block_count=counts["block_count"],
                    clause_count=counts["clause_count"],
                    warning_count=(
                        len(existing["document"].get("parse_warnings") or []) + len(structure_warnings)
                    ),
                    clause_profile=existing["document"].get("structure_profile") or "unknown",
                )
                return {**existing, "reused": True, "ingestion_run_id": run_id}

            stage = "parse"
            self.repository.update_ingestion_run(run_id, stage=stage)
            parsed = self.parser.parse(content, original_name)

            stage = "split"
            self.repository.update_ingestion_run(
                run_id,
                stage=stage,
                block_count=len(parsed.blocks),
                warning_count=len(parsed.warnings),
            )
            extraction = self.extractor.extract(parsed.blocks)
            units = extraction.units
            total_warning_count = len(parsed.warnings) + len(extraction.warnings)
            self.repository.update_ingestion_run(
                run_id,
                stage=stage,
                clause_count=len(units),
                warning_count=total_warning_count,
                clause_profile=extraction.profile,
            )

            policy_id = uuid.uuid4().hex
            document_id = uuid.uuid4().hex
            relative_path = (
                Path("tenants")
                / tenant_storage_key()
                / "files"
                / content_hash[:2]
                / f"{content_hash}{suffix}"
            )
            target = self.storage_root / relative_path

            stage = "store"
            self.repository.update_ingestion_run(run_id, stage=stage, clause_count=len(units))
            target.parent.mkdir(parents=True, exist_ok=True)
            created_file = not target.exists()
            if created_file:
                target.write_bytes(content)

            stage = "persist"
            self.repository.update_ingestion_run(run_id, stage=stage)
            policy_title = (title or Path(original_name).stem).strip()
            normalized_title = normalize_policy_title(policy_title)
            requested_category = (category_code or "auto").strip()
            resolved_category = (
                infer_policy_category(policy_title)
                if requested_category == "auto"
                else requested_category
            )
            document_text = "\n".join(unit.text for unit in units)
            normalized_document = normalize_similarity_text(document_text)
            candidate_loader = getattr(self.repository, "list_similarity_candidates", None)
            candidates = (
                candidate_loader(
                    normalized_title=normalized_title,
                    category_code=resolved_category,
                    normalized_text_length=len(normalized_document),
                    title_threshold=self.settings.similarity_title_threshold,
                )
                if candidate_loader is not None
                else []
            )
            similarity = similarity_report(
                title=policy_title,
                normalized_title=normalized_title,
                category_code=resolved_category,
                text=document_text,
                clauses=[unit.text for unit in units],
                candidates=candidates,
                thresholds=SimilarityThresholds(
                    title=self.settings.similarity_title_threshold,
                    edit=self.settings.similarity_edit_threshold,
                    jaccard=self.settings.similarity_jaccard_threshold,
                    containment=self.settings.similarity_containment_threshold,
                    length_ratio=self.settings.similarity_length_ratio_threshold,
                    clause_coverage=self.settings.similarity_clause_coverage_threshold,
                ),
                limit=self.settings.similarity_candidate_limit,
            )
            payload = self.repository.ingest(
                policy_id=policy_id,
                document_id=document_id,
                title=policy_title,
                normalized_title=normalized_title,
                version=DEFAULT_VERSION,
                version_seq=0,
                family_id=policy_id,
                similarity_state=similarity["status"],
                similarity_report=similarity,
                level_code=(level_code or "").strip() or None,
                category_code=resolved_category,
                content_hash=content_hash,
                normalized_text_hash=normalized_text_hash(document_text),
                normalized_text_length=len(normalized_document),
                original_name=original_name,
                file_type=parsed.file_type,
                storage_path=str(relative_path),
                parser_version=self.parser.version,
                chunker_version=self.extractor.version,
                warnings=parsed.warnings,
                structure_profile=extraction.profile,
                structure_diagnostics={
                    **extraction.diagnostics,
                    "warnings": extraction.warnings,
                },
                blocks=parsed.blocks,
                units=units,
                ingestion_run_id=run_id,
                ingestion_warning_count=total_warning_count,
            )
            return {
                **payload,
                "similarity": similarity,
                "reused": False,
                "ingestion_run_id": run_id,
            }
        except ProofError as exc:
            self._remove_created_file(target, created_file)
            self._record_failure(
                run_id,
                stage=stage,
                error_code=exc.code,
                error_message=str(exc),
                error_details=exc.details,
            )
            raise _error_with_run_id(exc, run_id) from exc
        except Exception as exc:
            self._remove_created_file(target, created_file)
            logger.exception("Unexpected policy ingestion failure for run %s", run_id)
            self._record_failure(
                run_id,
                stage=stage,
                error_code="ingestion_failed",
                error_message="Policy ingestion failed.",
                error_details={},
            )
            raise ProofError(
                "ingestion_failed",
                "Policy ingestion failed.",
                status_code=500,
                details={"ingestion_run_id": run_id},
            ) from exc

    def _validate_input(self, content: bytes, filename: str) -> tuple[str, str]:
        if not content:
            raise ProofError("empty_document", "Uploaded document is empty.", status_code=422)
        if len(content) > self.settings.max_upload_bytes:
            raise ProofError(
                "file_too_large",
                "Uploaded document exceeds the configured size limit.",
                status_code=413,
                details={"max_upload_bytes": self.settings.max_upload_bytes},
            )
        original_name = _safe_filename(filename)
        suffix = Path(original_name).suffix.lower()
        if suffix not in self.parser.supported_extensions:
            raise ProofError(
                "unsupported_file_type",
                f"Unsupported file type: {suffix or '<none>'}",
                status_code=415,
                details={"supported": sorted(self.parser.supported_extensions)},
            )
        return original_name, suffix

    def _record_failure(
        self,
        run_id: str,
        *,
        stage: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any],
    ) -> None:
        try:
            self.repository.fail_ingestion_run(
                run_id,
                stage=stage,
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
            )
        except Exception:
            logger.exception("Unable to mark policy ingestion run %s as failed", run_id)

    @staticmethod
    def _remove_created_file(target: Path | None, created_file: bool) -> None:
        if target is not None and created_file:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.exception("Unable to clean up policy source file %s", target)


def _safe_filename(filename: str) -> str:
    base = Path(filename or "policy.txt").name
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._()（） -]+", "_", base).strip()
    return value[:200] or "policy.txt"


def _error_with_run_id(exc: ProofError, run_id: str) -> ProofError:
    return ProofError(
        exc.code,
        str(exc),
        status_code=exc.status_code,
        details={**exc.details, "ingestion_run_id": run_id},
    )
