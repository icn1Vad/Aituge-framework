from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from proof.application.dataset_audit import DatasetAuditor
from proof.application.conflict_retrieval import (
    ConflictRetrievalLimits,
    ConflictRetrievalService,
)
from proof.application.conflict_retrieval.service import CONFLICT_RERANK_INSTRUCTION
from proof.application.ingestion import PolicyIngestionPipeline
from proof.application.intra_conflict_retrieval import IntraConflictRetrievalService
from proof.application.quality_report import build_policy_quality_report
from proof.application.retrieval import HybridPolicyRetriever, RetrievalFilters
from proof.application.semantic_audit import (
    FindingValidationResult,
    IntraConflictValidationResult,
    PolicyAuditService,
)
from proof.application.short_refs import ref_id_map
from proof.application.sql_query import PolicySqlQueryService
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.postgres.repository import ProofRepository
from proof.infrastructure.reranking import DashScopePolicyReranker
from proof.infrastructure.retrieval import PgvectorPolicyRetriever, PostgresKeywordRetriever


logger = logging.getLogger(__name__)
POLICY_LEVEL_NAMES = {"upper": "一级制度", "peer": "二级制度", "lower": "三级制度"}
POLICY_LEVEL_RANKS = {"upper": 300, "peer": 200, "lower": 100}
INTRA_CONFLICT_TYPES = {
    "numeric_conflict",
    "authority_conflict",
    "process_conflict",
    "rule_reversal",
}


class ProofService:
    def __init__(
        self,
        settings: Settings,
        repository: ProofRepository | None = None,
        ingestion_pipeline: PolicyIngestionPipeline | None = None,
        dataset_auditor: DatasetAuditor | None = None,
        retrieval_pipeline: HybridPolicyRetriever | None = None,
        sql_query_service: PolicySqlQueryService | None = None,
        policy_audit_service: PolicyAuditService | None = None,
        conflict_retrieval_service: ConflictRetrievalService | None = None,
        intra_conflict_retrieval_service: IntraConflictRetrievalService | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or ProofRepository(settings)
        self.storage_root = settings.resolved_storage_root()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.ingestion_pipeline = ingestion_pipeline or PolicyIngestionPipeline(settings, self.repository)
        dataset_root = settings.resolved_dataset_root()
        self.dataset_auditor = dataset_auditor or (
            DatasetAuditor(dataset_root, max_upload_bytes=settings.max_upload_bytes) if dataset_root else None
        )
        self.retrieval_pipeline = retrieval_pipeline or HybridPolicyRetriever(
            keyword=PostgresKeywordRetriever(self.repository),
            vector=PgvectorPolicyRetriever(settings, self.repository),
            reranker=DashScopePolicyReranker(settings) if settings.reranker_configured else None,
            keyword_limit=settings.retrieval_keyword_limit,
            vector_limit=settings.retrieval_vector_limit,
            rerank_candidate_limit=settings.rerank_candidate_limit,
        )
        self.sql_query_service = sql_query_service or PolicySqlQueryService(settings, self.repository)
        self.intra_conflict_retrieval_service = intra_conflict_retrieval_service or IntraConflictRetrievalService(
            repository=self.repository,
            settings=self.settings,
        )
        self.policy_audit_service = policy_audit_service or PolicyAuditService(
            settings,
            self.repository,
            intra_conflict_retrieval_service=self.intra_conflict_retrieval_service,
        )
        self.conflict_retrieval_service = conflict_retrieval_service

    def health(self) -> dict[str, Any]:
        storage_ok = self.storage_root.is_dir() and os.access(self.storage_root, os.W_OK)
        database = self.repository.health()
        return {
            "ok": bool(database.get("ok") and storage_ok),
            "service": "proof",
            "database": database,
            "storage": {"ok": storage_ok, "root": str(self.storage_root)},
            "embedding_configured": self.settings.embedding_configured,
            "semantic_audit": {
                "enabled": self.settings.semantic_audit_enabled,
                "framework_configured": bool(self.settings.framework_base_url.strip()),
                "model": self.settings.audit_model_id if self.settings.semantic_audit_enabled else None,
            },
            "reranker": {
                "configured": self.settings.reranker_configured,
                "model": self.settings.rerank_model if self.settings.reranker_configured else None,
            },
        }

    def ingest_policy(
        self,
        *,
        content: bytes,
        filename: str,
        title: str = "",
        version: str = "1.0",
        level_code: str | None = None,
        category_code: str = "auto",
    ) -> dict[str, Any]:
        result = self.ingestion_pipeline.ingest_policy(
            content=content,
            filename=filename,
            title=title,
            version=version,
            level_code=level_code,
            category_code=category_code,
        )
        policy = result["policy"]
        document = result["document"]
        if policy.get("status") == "draft" or result.get("reused"):
            audit_state = self.policy_audit_service.ensure_dispatched(document["id"])
        else:
            audit_state = self._semantic_state_for_policy(policy, document["id"])
        result["audit_task"] = _audit_task_view(audit_state)
        return result

    def get_ingestion_run(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_ingestion_run(run_id)
        if not run:
            raise ProofError("ingestion_run_not_found", "Ingestion run not found.", status_code=404)
        return run

    def audit_dataset(self, *, refresh: bool = False) -> dict[str, Any]:
        auditor = self._dataset_auditor()
        snapshot = auditor.audit(refresh=refresh)
        documents = self.repository.get_documents_by_content_hashes(
            [item["content_hash"] for item in snapshot["files"]]
        )
        for item in snapshot["files"]:
            item["ingestion"] = _dataset_ingestion(item, documents.get(item["content_hash"]))
        snapshot["summary"]["ingested_file_count"] = sum(
            item["ingestion"]["status"] == "ingested" for item in snapshot["files"]
        )
        return snapshot

    def get_dataset_file(self, file_id: str) -> dict[str, Any]:
        item = self._dataset_auditor().file_detail(file_id)
        documents = self.repository.get_documents_by_content_hashes([item["content_hash"]])
        item["ingestion"] = _dataset_ingestion(item, documents.get(item["content_hash"]))
        return item

    def list_files(self) -> dict[str, Any]:
        items = self.repository.list_files()
        return {"items": items, "total": len(items)}

    def get_file_content(self, file_id: str) -> dict[str, Any]:
        item = self._get_file(file_id)
        if str(item.get("file_type") or "").lower().lstrip(".") != "pdf":
            raise ProofError(
                "file_content_unsupported",
                "Only PDF files can be read through this endpoint.",
                status_code=415,
                details={"file_id": file_id, "file_type": item.get("file_type")},
            )

        path = (self.storage_root / Path(str(item["storage_path"]))).resolve()
        if not path.is_relative_to(self.storage_root):
            raise ProofError(
                "invalid_file_storage_path",
                "The stored file path is outside the Proof storage root.",
                status_code=500,
                details={"file_id": file_id},
            )
        if not path.is_file():
            raise ProofError(
                "file_content_not_found",
                "The PDF file content could not be found.",
                status_code=404,
                details={"file_id": file_id},
            )
        return {"path": path, "name": item["name"], "media_type": "application/pdf"}

    def list_file_chunks(self, file_id: str, *, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        if limit < 1 or limit > 10:
            raise ProofError(
                "invalid_chunk_limit",
                "Chunk limit must be between 1 and 10.",
                status_code=422,
                details={"max_limit": 10},
            )
        if offset < 0:
            raise ProofError("invalid_chunk_offset", "Chunk offset must not be negative.", status_code=422)

        item = self._get_file(file_id)
        chunks = self.repository.list_file_chunks(file_id, limit=limit, offset=offset)
        total = int(item.get("chunk_count") or 0)
        return {
            "file_id": file_id,
            "items": chunks,
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": offset + len(chunks) < total,
        }

    def _get_file(self, file_id: str) -> dict[str, Any]:
        item = self.repository.get_file(file_id)
        if item is None:
            raise ProofError("file_not_found", "File not found.", status_code=404)
        return item

    def _dataset_auditor(self) -> DatasetAuditor:
        if self.dataset_auditor is None:
            raise ProofError(
                "dataset_unconfigured",
                "PROOF_DATASET_ROOT is not configured.",
                status_code=503,
            )
        return self.dataset_auditor

    def list_policies(self, **filters: Any) -> list[dict[str, Any]]:
        return self.repository.list_policies(**filters)

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        policy = self.repository.get_policy(policy_id)
        if not policy:
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        return policy

    def list_clauses(self, policy_id: str, *, include_text: bool = False) -> list[dict[str, Any]]:
        self.get_policy(policy_id)
        return self.repository.list_clauses(policy_id, include_text=include_text)

    def get_audit_status(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        document_id = policy["document_id"]
        semantic = self._semantic_state_for_policy(policy, document_id)
        summary = self._summary_state_for_policy(policy, document_id)
        conflict = self._conflict_state_for_policy(policy, document_id)
        intra_conflict = self._intra_conflict_state_for_policy(policy, document_id)
        stage_states = {
            "policy_summary": _stage_status(summary),
            "semantic_audit": _stage_status(semantic),
            "conflict_audit": _stage_status(conflict),
            "intra_conflict_audit": _stage_status(intra_conflict),
        }
        clauses = self.repository.list_clauses(policy_id, include_text=True)
        report = build_policy_quality_report(
            policy,
            clauses,
            semantic_audit=semantic,
            semantic_findings=self.policy_audit_service.findings(document_id),
            conflict_audit=conflict,
            conflict_findings=self.policy_audit_service.conflict_findings(document_id),
            intra_conflict_audit=intra_conflict,
            intra_conflict_findings=self.policy_audit_service.intra_conflict_findings(document_id),
        )
        semantic_ready = semantic.get("status") == "completed"
        conflict_ready = conflict.get("status") == "completed"
        intra_ready = intra_conflict.get("status") == "completed"
        finding_counts = report["finding_counts"]
        conflict_counts = report["conflict_counts"]
        intra_counts = report["intra_conflict_counts"]
        status = _combined_audit_status(*(stage["status"] for stage in stage_states.values()))
        return {
            "id": semantic.get("id"),
            "policy_id": policy_id,
            "framework_task_id": semantic.get("framework_task_id"),
            "framework_run_id": semantic.get("framework_run_id"),
            "status": status,
            "can_confirm": (
                semantic.get("status") in {"completed", "disabled", "not_requested"}
                and conflict.get("status") in {"completed", "disabled", "not_requested"}
                and intra_conflict.get("status") in {"completed", "disabled", "not_requested"}
            ),
            "stages": stage_states,
            "counts": {
                "clause_total": len(clauses),
                "semantic_ambiguity": finding_counts["semantic_ambiguity"] if semantic_ready else None,
                "executability_gap": finding_counts["executability_gap"] if semantic_ready else None,
                "duplicate_number": finding_counts["duplicate_number"],
                "missing_number": finding_counts["missing_number"],
                "mixed_structure": finding_counts["mixed_structure"],
                "conflict_total": conflict_counts["total"] if conflict_ready else None,
                "numeric_conflict": conflict_counts["numeric_conflict"] if conflict_ready else None,
                "authority_conflict": conflict_counts["authority_conflict"] if conflict_ready else None,
                "process_conflict": conflict_counts["process_conflict"] if conflict_ready else None,
                "rule_reversal": conflict_counts["rule_reversal"] if conflict_ready else None,
                "intra_conflict_total": intra_counts["total"] if intra_ready else None,
                "intra_numeric_conflict": intra_counts["numeric_conflict"] if intra_ready else None,
                "intra_authority_conflict": intra_counts["authority_conflict"] if intra_ready else None,
                "intra_process_conflict": intra_counts["process_conflict"] if intra_ready else None,
                "intra_rule_reversal": intra_counts["rule_reversal"] if intra_ready else None,
            },
        }

    def get_policy_summary(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        return self._summary_state_for_policy(policy, policy["document_id"])

    def get_semantic_findings(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        document_id = policy["document_id"]
        audit = self._semantic_state_for_policy(policy, document_id)
        report = build_policy_quality_report(
            policy,
            self.repository.list_clauses(policy_id, include_text=True),
            semantic_audit=audit,
            semantic_findings=self.policy_audit_service.findings(document_id),
        )
        return {
            **_stage_status(audit),
            "findings": report["findings"],
        }

    def get_conflict_findings(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        document_id = policy["document_id"]
        audit = self._conflict_state_for_policy(policy, document_id)
        findings = self.policy_audit_service.conflict_findings(document_id)
        return {
            **_stage_status(audit),
            "findings": self._with_conflict_candidate_availability(findings),
        }

    def get_intra_conflict_findings(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        document_id = policy["document_id"]
        audit = self._intra_conflict_state_for_policy(policy, document_id)
        return {
            **_stage_status(audit),
            "findings": self.policy_audit_service.intra_conflict_findings(document_id),
        }

    def _with_conflict_candidate_availability(
        self,
        findings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidate_ids = list(
            dict.fromkeys(
                str(candidate_id)
                for finding in findings
                for candidate_id in (finding.get("candidate_ids") or [])
            )
        )
        available_ids = {
            str(unit["id"])
            for unit in self.repository.fetch_units(candidate_ids)
        }
        return [
            {
                **finding,
                "unavailable_candidate_ids": [
                    str(candidate_id)
                    for candidate_id in (finding.get("candidate_ids") or [])
                    if str(candidate_id) not in available_ids
                ],
            }
            for finding in findings
        ]

    def _summary_state_for_policy(self, policy: dict[str, Any], document_id: str) -> dict[str, Any]:
        state = self.policy_audit_service.summary_state(document_id)
        if policy.get("status") == "effective" and state.get("status") == "pending":
            return {"status": "not_requested", "error_message": None, "content": None}
        return state

    def _semantic_state_for_policy(self, policy: dict[str, Any], document_id: str) -> dict[str, Any]:
        state = self.policy_audit_service.get_state(document_id)
        if policy.get("status") == "effective" and state.get("status") == "pending" and not state.get("id"):
            return {"status": "not_requested", "error_message": None}
        return state

    def _conflict_state_for_policy(self, policy: dict[str, Any], document_id: str) -> dict[str, Any]:
        state = self.policy_audit_service.conflict_state(document_id)
        if policy.get("status") == "effective" and state.get("status") == "pending":
            return {"status": "not_requested", "error_message": None}
        return state

    def _intra_conflict_state_for_policy(
        self, policy: dict[str, Any], document_id: str
    ) -> dict[str, Any]:
        state = self.policy_audit_service.intra_conflict_state(document_id)
        if policy.get("status") == "effective" and state.get("status") == "pending":
            return {"status": "not_requested", "error_message": None}
        return state

    def accept_semantic_audit_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.policy_audit_service.accept_result(
            payload,
            conflict_output_validator=self._validate_conflict_output,
            intra_conflict_output_validator=self._validate_intra_conflict_output,
        )

    def confirm_policy(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        if policy.get("status") == "effective":
            return policy
        if policy.get("status") != "draft":
            raise ProofError(
                "policy_not_draft",
                "Only a draft policy can be confirmed.",
                status_code=409,
            )
        if self.settings.semantic_audit_enabled:
            audit = self.policy_audit_service.get_state(policy["document_id"])
            if audit["status"] != "completed":
                raise ProofError(
                    "semantic_audit_incomplete",
                    "Semantic review must complete before the policy can be confirmed.",
                    status_code=409,
                    details={"semantic_audit": audit},
                )
            conflict = self.policy_audit_service.conflict_state(policy["document_id"])
            if conflict["status"] != "completed":
                raise ProofError(
                    "conflict_audit_incomplete",
                    "Conflict review must complete before the policy can be confirmed.",
                    status_code=409,
                    details={"conflict_audit": conflict},
                )
            intra_conflict = self._intra_conflict_state_for_policy(policy, policy["document_id"])
            if intra_conflict["status"] != "completed":
                raise ProofError(
                    "intra_conflict_audit_incomplete",
                    "Intra-policy conflict review must complete before the policy can be confirmed.",
                    status_code=409,
                    details={"intra_conflict_audit": intra_conflict},
                )
        confirmed = self.repository.confirm_policy(policy_id)
        if confirmed is None:
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        if confirmed.get("status") != "effective":
            raise ProofError(
                "policy_not_draft",
                "Only a draft policy can be confirmed.",
                status_code=409,
            )
        return confirmed

    def discard_policy(self, policy_id: str) -> dict[str, Any]:
        deleted = self.repository.delete_draft_policy(policy_id)
        if deleted is None:
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        self._remove_stored_file(
            deleted["storage_path"],
            warning="Draft %s was deleted but its source file could not be removed.",
            resource_id=policy_id,
        )
        return {"id": policy_id, "status": "discarded"}

    def _remove_stored_file(self, relative_path: str, *, warning: str, resource_id: str) -> None:
        storage_path = (self.storage_root / Path(relative_path)).resolve()
        if storage_path.is_relative_to(self.storage_root) and storage_path.is_file():
            try:
                storage_path.unlink()
            except OSError:
                logger.warning(warning, resource_id)

    def list_levels(self) -> list[dict[str, Any]]:
        return self.repository.list_levels()

    def list_categories(self) -> list[dict[str, Any]]:
        return self.repository.list_categories()

    def fetch_units(self, unit_ids: list[str]) -> list[dict[str, Any]]:
        results = []
        for unit in self.repository.fetch_units(unit_ids):
            enriched = _with_citation(unit)
            results.append(
                {
                    "id": enriched["id"],
                    "text": enriched["text"],
                    "citation": enriched["citation"],
                }
            )
        return results

    def index_document(self, document_id: str) -> dict[str, Any]:
        policy = self.repository.get_policy_by_document_id(document_id)
        if policy is None:
            raise ProofError("document_not_found", "Document or clauses not found.", status_code=404)
        if policy.get("status") != "effective":
            raise ProofError(
                "policy_not_effective",
                "A draft policy cannot be indexed.",
                status_code=409,
            )
        client = OpenAICompatibleEmbeddingClient(self.settings)
        units = self.repository.get_document_units(document_id)
        if not units:
            raise ProofError("document_not_found", "Document or clauses not found.", status_code=404)
        candidates: list[dict[str, Any]] = []
        too_long_ids: list[str] = []
        for unit in units:
            limit = self.settings.embedding_max_input_chars
            if limit is not None and len(unit["text"]) > limit:
                too_long_ids.append(unit["id"])
            else:
                candidates.append(unit)
        try:
            indexed_units, vectors, rejected_ids = _embed_preserving_whole_clauses(client, candidates)
            too_long_ids.extend(rejected_ids)
            self.repository.replace_embeddings(
                document_id,
                indexed_units,
                vectors,
                client.profile,
                too_long_unit_ids=too_long_ids,
            )
        except ProofError as exc:
            self.repository.mark_embedding_failed(document_id, exc.code)
            raise
        return {
            "document_id": document_id,
            "status": "indexed",
            "unit_count": len(units),
            "indexed_unit_count": len(indexed_units),
            "embedding_too_long_count": len(too_long_ids),
            "embedding_profile": {
                "id": client.profile.id,
                "provider": client.profile.provider,
                "model": client.profile.model,
                "dimensions": client.profile.dimensions,
            },
        }

    def search(
        self,
        *,
        query: str,
        top_k: int = 8,
        retrieval_mode: str = "hybrid",
        policy_ids: list[str] | None = None,
        level_codes: list[str] | None = None,
        category_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ProofError("empty_query", "Search query is empty.", status_code=422)
        payload = self.retrieval_pipeline.search(
            query.strip(),
            top_k=max(1, min(int(top_k or 8), 20)),
            mode=retrieval_mode,
            filters=RetrievalFilters(
                policy_ids=policy_ids or [],
                level_codes=level_codes or [],
                category_codes=category_codes or [],
            ),
        )
        vector_metadata = payload.get("branch_metadata", {}).get("vector", {})
        payload["embedding_profile"] = vector_metadata.get("embedding_profile")
        payload["results"] = [_with_citation(result) for result in payload["results"]]
        return payload

    def retrieve_conflict_candidates(self, unit_id: str, *, top_k: int = 10) -> dict[str, Any]:
        if self.conflict_retrieval_service is None:
            conflict_settings = self.settings.model_copy(
                update={"rerank_instruction": CONFLICT_RERANK_INSTRUCTION}
            )
            self.conflict_retrieval_service = ConflictRetrievalService(
                repository=self.repository,
                embedding_client=OpenAICompatibleEmbeddingClient(self.settings),
                reranker=(
                    DashScopePolicyReranker(conflict_settings)
                    if conflict_settings.reranker_configured
                    else None
                ),
                limits=ConflictRetrievalLimits(
                    same_title=self.settings.conflict_same_title_limit,
                    leaf_category=self.settings.conflict_leaf_category_limit,
                    parent_category=self.settings.conflict_parent_category_limit,
                    global_recall=self.settings.conflict_global_limit,
                    max_candidates=self.settings.conflict_max_candidates,
                ),
            )
        return self.conflict_retrieval_service.retrieve_for_unit(unit_id, top_k=top_k).to_dict()

    def retrieve_intra_conflict_candidates(self, unit_id: str) -> dict[str, Any]:
        return self.intra_conflict_retrieval_service.retrieve_for_unit(unit_id)

    def accept_conflict_audit_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        findings = self._validate_conflict_output(payload)
        return {
            "audit_id": str(payload.get("audit_id") or ""),
            "status": "validated",
            "finding_count": len(findings),
        }

    def _load_conflict_candidate_ref_map(self, source_id: str) -> dict[str, str]:
        retrieval = self.retrieve_conflict_candidates(source_id, top_k=10)
        source = retrieval.get("source") if isinstance(retrieval, dict) else None
        if not isinstance(source, dict) or str(source.get("id") or "") != source_id:
            raise ProofError(
                "conflict_retrieval_mismatch",
                "Cross-policy retrieval source does not match the current item target.",
                status_code=422,
                details={"target_id": source_id},
            )
        try:
            return ref_id_map(retrieval.get("results") or [])
        except ValueError as exc:
            raise ProofError(
                "invalid_conflict_retrieval_refs",
                str(exc),
                status_code=422,
                details={"target_id": source_id},
            ) from exc

    def _resolve_conflict_candidate_refs(
        self,
        source_id: str,
        raw_refs: Any,
        *,
        ref_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        if not isinstance(raw_refs, list):
            raise ProofError(
                "invalid_conflict_result",
                "Cross-policy candidate_refs must be a list.",
                status_code=422,
            )
        candidate_refs = [str(value).strip().upper() for value in raw_refs]
        if (
            not candidate_refs
            or len(candidate_refs) > 4
            or len(candidate_refs) != len(set(candidate_refs))
            or any(
                len(value) != 3
                or not value.startswith("C")
                or not value[1:].isdigit()
                or not 1 <= int(value[1:]) <= 10
                for value in candidate_refs
            )
        ):
            raise ProofError(
                "invalid_conflict_result",
                "Candidate refs must be unique values from C01 through C10.",
                status_code=422,
                details={"target_id": source_id, "candidate_refs": candidate_refs},
            )

        mapping = (
            ref_mapping
            if ref_mapping is not None
            else self._load_conflict_candidate_ref_map(source_id)
        )
        invalid_refs = [value for value in candidate_refs if value not in mapping]
        if invalid_refs:
            raise ProofError(
                "conflict_candidate_ref_not_found",
                "Candidate refs must come from the current target retrieval results.",
                status_code=422,
                details={
                    "target_id": source_id,
                    "invalid_refs": invalid_refs,
                    "allowed_refs": sorted(mapping),
                },
            )
        return [mapping[value] for value in candidate_refs]

    def _validate_conflict_output(
        self, payload: dict[str, Any]
    ) -> FindingValidationResult:
        task_type = payload.get("task_type")
        if task_type not in {None, "proof.conflict.audit", "proof.audit.run"}:
            raise ProofError("invalid_conflict_result", "Unexpected conflict task type.", status_code=422)
        output = payload.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("items"), list):
            raise ProofError("invalid_conflict_result", "Conflict output items are required.", status_code=422)

        expected_source_ids: set[str] | None = None
        expected_document_id = ""
        if task_type == "proof.audit.run":
            audit_id = str(payload.get("audit_id") or "")
            run = self.repository.get_audit_run(audit_id)
            if run is None:
                raise ProofError("audit_run_not_found", "Audit run not found.", status_code=404)
            expected_document_id = str(run["document_id"])
            expected_source_ids = {
                str(unit["id"])
                for unit in self.repository.get_document_units(expected_document_id)
            }

        validated_findings: list[dict[str, Any]] = []
        covered_source_ids: list[str] = []
        finding_keys: set[tuple[str, tuple[str, ...], str]] = set()
        warning_count = 0
        for item in output["items"]:
            if not isinstance(item, dict):
                raise ProofError(
                    "invalid_conflict_result",
                    "Each conflict audit item must be an object.",
                    status_code=422,
                )
            target_items = ((item.get("input") or {}).get("targets") or [])
            target_id_list = [
                str(target.get("id"))
                for target in target_items
                if isinstance(target, dict) and target.get("id")
            ]
            if len(target_id_list) != 1:
                raise ProofError(
                    "invalid_conflict_result",
                    "Each integrated conflict item must contain exactly one source Chunk.",
                    status_code=422,
                )
            source_id = target_id_list[0]
            target_ids = {source_id}
            covered_source_ids.extend(target_id_list)
            if item.get("status") != "succeeded":
                warning_count += 1
                continue
            result_wrapper = item.get("result") or {}
            result = result_wrapper.get("result") if isinstance(result_wrapper, dict) else None
            findings = result.get("findings") if isinstance(result, dict) else None
            if not isinstance(findings, list):
                warning_count += 1
                continue
            ref_mapping = self._load_conflict_candidate_ref_map(source_id) if findings else {}
            for finding in findings:
                expected_fields = {
                    "candidate_refs", "conflict_type", "problem", "suggestion"
                }
                if not isinstance(finding, dict) or set(finding) != expected_fields:
                    warning_count += 1
                    continue
                try:
                    candidate_ids = self._resolve_conflict_candidate_refs(
                        source_id, finding.get("candidate_refs"), ref_mapping=ref_mapping
                    )
                    normalized = self._validate_conflict_finding(
                        {
                            "id": source_id,
                            "candidate_ids": candidate_ids,
                            "conflict_type": finding.get("conflict_type"),
                            "problem": finding.get("problem"),
                            "suggestion": finding.get("suggestion"),
                        },
                        target_ids=target_ids,
                        expected_document_id=expected_document_id,
                    )
                except ProofError as exc:
                    if exc.code not in {
                        "invalid_conflict_result",
                        "conflict_candidate_ref_not_found",
                        "conflict_target_mismatch",
                    }:
                        raise
                    warning_count += 1
                    continue
                key = (
                    normalized["id"],
                    tuple(sorted(normalized["candidate_ids"])),
                    str(normalized.get("conflict_type") or ""),
                )
                if key in finding_keys:
                    warning_count += 1
                    continue
                finding_keys.add(key)
                validated_findings.append(normalized)
        if expected_source_ids is not None and (
            len(covered_source_ids) != len(set(covered_source_ids))
            or set(covered_source_ids) != expected_source_ids
        ):
            raise ProofError(
                "invalid_conflict_result",
                "Conflict audit items did not cover every source Chunk exactly once.",
                status_code=422,
            )
        return FindingValidationResult(
            findings=validated_findings,
            warning_count=warning_count,
            warning_label="模型引用无法解析",
        )

    def _validate_conflict_finding(
        self,
        finding: Any,
        *,
        target_ids: set[str],
        expected_document_id: str = "",
    ) -> dict[str, Any]:
        if not isinstance(finding, dict) or str(finding.get("id") or "") not in target_ids:
            raise ProofError(
                "conflict_target_mismatch",
                "Conflict finding id is not a target in the current item.",
                status_code=422,
            )
        source_id = str(finding["id"])
        candidate_ids = list(dict.fromkeys(str(value) for value in (finding.get("candidate_ids") or [])))
        if not candidate_ids or source_id in candidate_ids:
            raise ProofError(
                "invalid_conflict_result",
                "Conflict candidate IDs must be non-empty and must not contain the source ID.",
                status_code=422,
            )
        units: dict[str, dict[str, Any]] = {}
        for unit_id in {source_id, *candidate_ids}:
            unit = self.repository.get_conflict_source_unit(unit_id)
            if unit is None:
                raise ProofError(
                    "conflict_unit_not_found",
                    "Conflict finding references an unknown Chunk ID.",
                    status_code=422,
                    details={"unit_id": unit_id},
                )
            units[unit_id] = unit

        if expected_document_id and str(units[source_id].get("document_id")) != expected_document_id:
            raise ProofError(
                "conflict_target_mismatch",
                "Conflict source does not belong to the audited document.",
                status_code=422,
            )
        for candidate_id in candidate_ids:
            if expected_document_id and units[candidate_id].get("policy_status") != "effective":
                raise ProofError(
                    "conflict_candidate_not_effective",
                    "Cross-policy conflict candidates must belong to effective policies.",
                    status_code=422,
                    details={"unit_id": candidate_id},
                )

        conflict_type = str(finding.get("conflict_type") or "")
        if conflict_type not in {
            "numeric_conflict", "authority_conflict", "process_conflict", "rule_reversal"
        }:
            raise ProofError("invalid_conflict_result", "Invalid conflict type.", status_code=422)
        text_fields = ("problem", "suggestion")
        if any(not str(finding.get(field) or "").strip() for field in text_fields):
            raise ProofError("invalid_conflict_result", "Conflict text fields must not be blank.", status_code=422)
        problem = str(finding["problem"]).strip()
        source_level = str(units[source_id].get("level_code") or "")
        cross_level_candidates = [
            candidate_id
            for candidate_id in candidate_ids
            if str(units[candidate_id].get("level_code") or "")
            and str(units[candidate_id].get("level_code") or "") != source_level
        ]
        if (
            source_level in POLICY_LEVEL_RANKS
            and cross_level_candidates
            and "层级关系：" not in problem
        ):
            candidate_context = "、".join(
                f"候选 Chunk {candidate_id} 为"
                f"{POLICY_LEVEL_NAMES.get(str(units[candidate_id].get('level_code') or ''), '未知层级')}"
                for candidate_id in cross_level_candidates
            )
            involved_levels = [
                source_level,
                *[
                    str(units[candidate_id].get("level_code") or "")
                    for candidate_id in cross_level_candidates
                ],
            ]
            highest_level = max(involved_levels, key=lambda code: POLICY_LEVEL_RANKS.get(code, -1))
            problem = (
                f"层级关系：当前制度为{POLICY_LEVEL_NAMES[source_level]}，{candidate_context}；"
                f"按一级制度 > 二级制度 > 三级制度，{POLICY_LEVEL_NAMES[highest_level]}优先。"
                f"{problem}"
            )
        return {
            "id": source_id,
            "candidate_ids": candidate_ids,
            "conflict_type": conflict_type,
            "problem": problem,
            "suggestion": str(finding["suggestion"]).strip(),
        }

    def _load_intra_conflict_candidate_ref_map(
        self, source_id: str
    ) -> dict[str, str]:
        retrieval = self.intra_conflict_retrieval_service.retrieve_for_unit(source_id)
        source = retrieval.get("source") if isinstance(retrieval, dict) else None
        if not isinstance(source, dict) or str(source.get("id") or "") != source_id:
            raise ProofError(
                "intra_conflict_retrieval_mismatch",
                "Intra-policy retrieval source does not match the current item target.",
                status_code=422,
                details={"target_id": source_id},
            )
        try:
            return ref_id_map(retrieval.get("results") or [])
        except ValueError as exc:
            raise ProofError(
                "invalid_intra_conflict_retrieval_refs",
                str(exc),
                status_code=422,
                details={"target_id": source_id},
            ) from exc

    def _resolve_intra_conflict_candidate_refs(
        self,
        source_id: str,
        raw_refs: Any,
        *,
        ref_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        if not isinstance(raw_refs, list):
            raise ProofError(
                "invalid_intra_conflict_result",
                "Intra-policy candidate_refs must be a list.",
                status_code=422,
            )
        candidate_refs = [str(value).strip().upper() for value in raw_refs]
        if (
            not candidate_refs
            or len(candidate_refs) > 4
            or len(candidate_refs) != len(set(candidate_refs))
            or any(
                len(value) != 3
                or not value.startswith("C")
                or not value[1:].isdigit()
                or not 1 <= int(value[1:]) <= 10
                for value in candidate_refs
            )
        ):
            raise ProofError(
                "invalid_intra_conflict_result",
                "Candidate refs must be unique values from C01 through C10.",
                status_code=422,
                details={"target_id": source_id, "candidate_refs": candidate_refs},
            )
        mapping = (
            ref_mapping
            if ref_mapping is not None
            else self._load_intra_conflict_candidate_ref_map(source_id)
        )
        invalid_refs = [value for value in candidate_refs if value not in mapping]
        if invalid_refs:
            raise ProofError(
                "intra_conflict_candidate_ref_not_found",
                "Candidate refs must come from the current target retrieval results.",
                status_code=422,
                details={
                    "target_id": source_id,
                    "invalid_refs": invalid_refs,
                    "allowed_refs": sorted(mapping),
                },
            )
        return [mapping[value] for value in candidate_refs]

    @staticmethod
    def _unresolved_intra_conflict_warning(
        *,
        target_id: str,
        item_index: int,
        finding_index: int | None,
        reason: ProofError,
    ) -> dict[str, Any]:
        return {
            "code": "model_reference_unresolved",
            "message": "模型引用无法解析",
            "reason_code": reason.code,
            "target_id": target_id,
            "item_index": item_index,
            "finding_index": finding_index,
        }

    def _normalize_intra_conflict_finding(
        self,
        *,
        target_id: str,
        finding: dict[str, Any],
        expected_ids: set[str],
        ordinal_by_id: dict[str, int],
        ref_mapping: dict[str, str] | None,
        document_id: str,
    ) -> dict[str, Any]:
        fields = set(finding)
        ref_contract = {
            "candidate_refs", "conflict_type", "problem", "suggestion"
        }
        legacy_contract = {
            "id", "candidate_ids", "conflict_type", "problem", "suggestion"
        }
        if fields == ref_contract:
            source_id = target_id
            candidate_ids = self._resolve_intra_conflict_candidate_refs(
                source_id,
                finding.get("candidate_refs"),
                ref_mapping=ref_mapping,
            )
        elif fields == legacy_contract:
            source_id = str(finding.get("id") or "")
            raw_candidates = finding.get("candidate_ids")
            if source_id != target_id or not isinstance(raw_candidates, list):
                raise ProofError(
                    "intra_conflict_target_mismatch",
                    "Finding id must match the current item target.",
                    status_code=422,
                )
            candidate_ids = [str(value) for value in raw_candidates]
            if (
                not candidate_ids
                or len(candidate_ids) > 4
                or len(candidate_ids) != len(set(candidate_ids))
                or source_id in candidate_ids
            ):
                raise ProofError(
                    "invalid_intra_conflict_result",
                    "Candidate IDs must be non-empty, unique, and exclude the source ID.",
                    status_code=422,
                )
        else:
            raise ProofError(
                "invalid_intra_conflict_result",
                "Each intra-policy conflict finding must use the ref contract.",
                status_code=422,
                details={"fields": sorted(fields), "target_id": target_id},
            )

        all_ids = {source_id, *candidate_ids}
        unexpected_ids = sorted(all_ids - expected_ids)
        if unexpected_ids:
            raise ProofError(
                "intra_conflict_unit_not_found",
                "All finding Chunk IDs must belong to the audited document.",
                status_code=422,
                details={
                    "document_id": document_id,
                    "target_id": target_id,
                    "unexpected_ids": unexpected_ids,
                },
            )
        conflict_type = str(finding.get("conflict_type") or "")
        if conflict_type not in INTRA_CONFLICT_TYPES:
            raise ProofError(
                "invalid_intra_conflict_result",
                "Invalid conflict type.",
                status_code=422,
            )
        if any(
            not str(finding.get(field) or "").strip()
            for field in ("problem", "suggestion")
        ):
            raise ProofError(
                "invalid_intra_conflict_result",
                "Conflict text fields must not be blank.",
                status_code=422,
            )
        ordered_ids = sorted(
            all_ids, key=lambda value: (ordinal_by_id[value], value)
        )
        return {
            "id": ordered_ids[0],
            "candidate_ids": ordered_ids[1:],
            "conflict_type": conflict_type,
            "problem": str(finding["problem"]).strip(),
            "suggestion": str(finding["suggestion"]).strip(),
        }

    def _validate_intra_conflict_output(
        self, payload: dict[str, Any]
    ) -> IntraConflictValidationResult:
        if payload.get("task_type") not in {None, "proof.audit.run"}:
            raise ProofError(
                "invalid_intra_conflict_result",
                "Unexpected intra-policy conflict task type.",
                status_code=422,
            )
        audit_id = str(payload.get("audit_id") or "")
        run = self.repository.get_audit_run(audit_id)
        if run is None:
            raise ProofError(
                "audit_run_not_found", "Audit run not found.", status_code=404
            )
        output = payload.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("items"), list):
            raise ProofError(
                "invalid_intra_conflict_result",
                "Intra-policy conflict output items are required.",
                status_code=422,
            )

        document_id = str(run["document_id"])
        units = self.repository.get_document_units(document_id)
        expected_ids = {str(unit["id"]) for unit in units}
        ordinal_by_id = {
            str(unit["id"]): (
                int(unit["clause_ordinal"])
                if unit.get("clause_ordinal") is not None
                else 2**31
            )
            for unit in units
        }
        covered_ids: list[str] = []
        normalized_by_key: dict[
            tuple[tuple[str, ...], str], dict[str, Any]
        ] = {}
        warnings: list[dict[str, Any]] = []

        for item_index, item in enumerate(output["items"]):
            if not isinstance(item, dict):
                raise ProofError(
                    "invalid_intra_conflict_result",
                    "Every intra-policy conflict audit item must be an object.",
                    status_code=422,
                )
            targets = ((item.get("input") or {}).get("targets") or [])
            target_ids = [
                str(target.get("id"))
                for target in targets
                if isinstance(target, dict) and target.get("id")
            ]
            if len(target_ids) != 1 or target_ids[0] not in expected_ids:
                raise ProofError(
                    "invalid_intra_conflict_result",
                    "Each intra-policy conflict item must contain exactly one audited source Chunk.",
                    status_code=422,
                )
            target_id = target_ids[0]
            covered_ids.append(target_id)

            if item.get("status") != "succeeded":
                warnings.append(
                    {
                        "code": "model_reference_unresolved",
                        "message": "模型引用无法解析",
                        "reason_code": str(
                            (item.get("error") or {}).get("code")
                            or "model_item_failed"
                        ),
                        "target_id": target_id,
                        "item_index": item_index,
                        "finding_index": None,
                    }
                )
                continue

            findings = (
                ((item.get("result") or {}).get("result") or {}).get("findings")
            )
            if not isinstance(findings, list):
                warnings.append(
                    {
                        "code": "model_reference_unresolved",
                        "message": "模型引用无法解析",
                        "reason_code": "invalid_intra_conflict_result",
                        "target_id": target_id,
                        "item_index": item_index,
                        "finding_index": None,
                    }
                )
                continue

            ref_mapping: dict[str, str] | None = None
            for finding_index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    warnings.append(
                        self._unresolved_intra_conflict_warning(
                            target_id=target_id,
                            item_index=item_index,
                            finding_index=finding_index,
                            reason=ProofError(
                                "invalid_intra_conflict_result",
                                "Each intra-policy conflict finding must be an object.",
                                status_code=422,
                            ),
                        )
                    )
                    continue
                if set(finding) == {
                    "candidate_refs", "conflict_type", "problem", "suggestion"
                } and ref_mapping is None:
                    ref_mapping = self._load_intra_conflict_candidate_ref_map(target_id)
                try:
                    normalized = self._normalize_intra_conflict_finding(
                        target_id=target_id,
                        finding=finding,
                        expected_ids=expected_ids,
                        ordinal_by_id=ordinal_by_id,
                        ref_mapping=ref_mapping,
                        document_id=document_id,
                    )
                except ProofError as exc:
                    warnings.append(
                        self._unresolved_intra_conflict_warning(
                            target_id=target_id,
                            item_index=item_index,
                            finding_index=finding_index,
                            reason=exc,
                        )
                    )
                    continue
                all_ids = {normalized["id"], *normalized["candidate_ids"]}
                key = (tuple(sorted(all_ids)), normalized["conflict_type"])
                normalized_by_key.setdefault(key, normalized)

        if len(covered_ids) != len(set(covered_ids)) or set(covered_ids) != expected_ids:
            raise ProofError(
                "invalid_intra_conflict_result",
                "Intra-policy conflict items must cover every Chunk exactly once.",
                status_code=422,
            )
        return IntraConflictValidationResult(
            findings=list(normalized_by_key.values()),
            warnings=warnings,
        )

    def execute_sql(self, *, question: str, sql: str) -> dict[str, Any]:
        return self.sql_query_service.execute(question=question, sql=sql)


def _with_citation(unit: dict[str, Any]) -> dict[str, Any]:
    clause_label = unit.get("clause_no_raw") or "未编号条款"
    citation_label = (
        f"[{unit.get('policy_title') or '未知制度'}｜{clause_label}｜"
        f"Chunk #{unit.get('clause_ordinal')}]"
    )
    return {
        **unit,
        "citation": {
            "policy_id": unit.get("policy_id"),
            "policy_title": unit.get("policy_title"),
            "policy_version": unit.get("policy_version"),
            "document_id": unit.get("document_id"),
            "original_name": unit.get("original_name"),
            "clause_no_raw": unit.get("clause_no_raw"),
            "clause_ordinal": unit.get("clause_ordinal"),
            "heading_path": unit.get("heading_path") or [],
            "page_start": unit.get("page_start"),
            "page_end": unit.get("page_end"),
            "label": citation_label,
        },
    }


def _stage_status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status") or "pending",
        "error_message": state.get("error_message"),
    }


def _combined_audit_status(*statuses: str) -> str:
    values = set(statuses)
    if "running" in values:
        return "running"
    if "pending" in values:
        return "pending"
    if "failed" in values:
        return "failed"
    return "completed"


def _audit_task_view(state: dict[str, Any]) -> dict[str, Any]:
    summary = state.get("policy_summary") or {}
    conflict = state.get("conflict_audit") or {}
    return {
        "id": state.get("id"),
        "framework_task_id": state.get("framework_task_id"),
        "framework_run_id": state.get("framework_run_id"),
        "status": _combined_audit_status(
            state.get("status") or "pending",
            summary.get("status") or state.get("status") or "pending",
            conflict.get("status") or state.get("status") or "pending",
        ),
    }


def _dataset_ingestion(item: dict[str, Any], document: dict[str, Any] | None) -> dict[str, Any]:
    if document:
        return {"status": "ingested", **document}
    if item["scan_status"] == "blocked":
        return {"status": "blocked"}
    return {"status": "not_ingested"}


def _embed_preserving_whole_clauses(
    client: OpenAICompatibleEmbeddingClient,
    units: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[float]], list[str]]:
    if not units:
        return [], [], []
    try:
        return units, client.embed([unit["text"] for unit in units]), []
    except ProofError as exc:
        if exc.code != "embedding_too_long":
            raise

    indexed_units: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    too_long_ids: list[str] = []
    for unit in units:
        try:
            vector = client.embed([unit["text"]])[0]
        except ProofError as exc:
            if exc.code != "embedding_too_long":
                raise
            too_long_ids.append(unit["id"])
            continue
        indexed_units.append(unit)
        vectors.append(vector)
    return indexed_units, vectors, too_long_ids
