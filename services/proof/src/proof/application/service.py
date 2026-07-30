from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from proof.application.conflict_retrieval import (
    ConflictRetrievalLimits,
    ConflictRetrievalService,
)
from proof.application.conflict_retrieval.service import CONFLICT_RERANK_INSTRUCTION
from proof.application.dataset_audit import DatasetAuditor
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
from proof.infrastructure.retrieval import (
    PgvectorPolicyRetriever,
    PostgresKeywordRetriever,
)
from proof.model_runtime import ProofModelRuntime, build_proof_model_runtime
from proof.tenant import current_tenant_id, tenant_storage_key

logger = logging.getLogger(__name__)
POLICY_LEVEL_NAMES = {"upper": "一级制度", "peer": "二级制度", "lower": "三级制度"}
POLICY_LEVEL_RANKS = {"upper": 300, "peer": 200, "lower": 100}
INTRA_CONFLICT_TYPES = {
    "numeric_conflict",
    "authority_conflict",
    "process_conflict",
    "rule_reversal",
}
POLICY_VERSION_PATTERN = r"^v[1-9]\d*\.[0-9]\.[0-9]$"
POLICY_LIFECYCLE_ACTIONS = ("activate", "expire", "discard", "delete")


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
        model_runtime: ProofModelRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.model_runtime = model_runtime or build_proof_model_runtime(settings)
        self.repository = repository or ProofRepository(settings)
        self.storage_root = settings.resolved_storage_root()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.ingestion_pipeline = ingestion_pipeline or PolicyIngestionPipeline(settings, self.repository)
        self.dataset_root = settings.resolved_dataset_root()
        self.dataset_auditor = dataset_auditor
        self._tenant_dataset_auditors: dict[str, DatasetAuditor] = {}
        self.retrieval_pipeline = retrieval_pipeline or HybridPolicyRetriever(
            keyword=PostgresKeywordRetriever(self.repository),
            vector=PgvectorPolicyRetriever(self.model_runtime.embedding, self.repository),
            reranker=(
                DashScopePolicyReranker(
                    self.model_runtime.reranker,
                    instruction=(
                        self.model_runtime.reranker.instruction
                        or settings.rerank_instruction
                    ),
                )
                if self.model_runtime.reranker_configured
                else None
            ),
            keyword_limit=settings.retrieval_keyword_limit,
            vector_limit=settings.retrieval_vector_limit,
            rerank_candidate_limit=settings.rerank_candidate_limit,
        )
        self.sql_query_service = sql_query_service or PolicySqlQueryService(settings, self.repository)
        self.intra_conflict_retrieval_service = intra_conflict_retrieval_service or IntraConflictRetrievalService(
            repository=self.repository,
            settings=self.settings,
            embedding_client=(
                OpenAICompatibleEmbeddingClient(self.model_runtime.embedding)
                if self.model_runtime.embedding_configured
                else None
            ),
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
            "model_pack": {
                "id": self.model_runtime.pack_id,
                "llm": self.model_runtime.llm_model_id,
                "embedding": (
                    self.model_runtime.embedding.id
                    if self.model_runtime.embedding is not None
                    else None
                ),
                "reranker": (
                    self.model_runtime.reranker.id
                    if self.model_runtime.reranker is not None
                    else None
                ),
            },
            "embedding_configured": self.model_runtime.embedding_configured,
            "semantic_audit": {
                "enabled": self.settings.semantic_audit_enabled,
                "framework_configured": bool(self.settings.framework_base_url.strip()),
                "model": (
                    self.model_runtime.llm_model_id
                    if self.settings.semantic_audit_enabled
                    else None
                ),
            },
            "reranker": {
                "configured": self.model_runtime.reranker_configured,
                "model": (
                    self.model_runtime.reranker.model
                    if self.model_runtime.reranker is not None
                    else None
                ),
                "mode": (
                    self.model_runtime.reranker.mode
                    if self.model_runtime.reranker is not None
                    else None
                ),
            },
        }

    def policy_metadata(self) -> dict[str, Any]:
        return {
            "levels": self.repository.list_levels(),
            "categories": self.repository.list_categories(),
            "supported_extensions": sorted(self.ingestion_pipeline.parser.supported_extensions),
            "max_upload_bytes": self.settings.max_upload_bytes,
            "version_pattern": POLICY_VERSION_PATTERN,
            "lifecycle_actions": list(POLICY_LIFECYCLE_ACTIONS),
        }

    def ingest_policy(
        self,
        *,
        content: bytes,
        filename: str,
        title: str = "",
        version: str = "v1.0.0",
        level_code: str | None = None,
        category_code: str = "auto",
        similarity_decision: str | None = None,
        candidate_policy_id: str | None = None,
        idempotency_key: str | None = None,
        dispatch_audit: bool = True,
    ) -> dict[str, Any]:
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key or len(normalized_key) > 128:
            raise ProofError(
                "invalid_idempotency_key",
                "Idempotency-Key is required and must not exceed 128 characters.",
                status_code=422,
            )
        decision = str(similarity_decision or "").strip().lower() or None
        if decision not in {None, "new_version", "separate"}:
            raise ProofError(
                "invalid_similarity_decision",
                "Similarity decision must be new_version or separate.",
                status_code=422,
            )
        candidate_id = str(candidate_policy_id or "").strip() or None
        if decision == "new_version" and not candidate_id:
            raise ProofError(
                "invalid_similarity_candidate",
                "candidate_policy_id is required for new_version.",
                status_code=422,
            )
        content_hash = hashlib.sha256(content).hexdigest()
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "content_hash": content_hash,
                    "filename": Path(filename or "policy.txt").name,
                    "title": str(title or "").strip(),
                    "version": str(version or "v1.0.0").strip(),
                    "level_code": str(level_code or "").strip(),
                    "category_code": str(category_code or "auto").strip(),
                    "similarity_decision": decision,
                    "candidate_policy_id": candidate_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reservation = self.repository.reserve_policy_create_request(
            idempotency_key=normalized_key,
            request_fingerprint=request_fingerprint,
        )
        if reservation.get("state") == "existing":
            if reservation.get("status") == "RUNNING":
                raise ProofError(
                    "policy_create_in_progress",
                    "The policy create request is still running.",
                    status_code=409,
                    details={"retryable": True},
                )
            policy_id = str(reservation.get("policy_id") or "")
            existing = self.repository.get_by_content_hash(content_hash)
            if (
                reservation.get("status") == "SUCCEEDED"
                and existing
                and str((existing.get("policy") or {}).get("id") or "") == policy_id
            ):
                return self._finalize_ingestion_result(
                    existing,
                    decision=decision,
                    candidate_policy_id=candidate_id,
                    idempotency_key=normalized_key,
                    replayed=True,
                    dispatch_audit=dispatch_audit,
                )
            raise ProofError(
                "policy_create_result_missing",
                "The idempotent policy result is no longer available.",
                status_code=409,
            )
        created_policy_id: str | None = None
        try:
            try:
                preview = self.preview_policy_similarity(
                    content=content,
                    filename=filename,
                    candidate_files=[],
                    title=title,
                    category_code=category_code,
                )
            except ProofError as preview_error:
                if preview_error.code in {
                    "empty_document",
                    "file_too_large",
                    "unsupported_file_type",
                    "document_parse_failed",
                    "no_clauses_found",
                }:
                    # The create endpoint must retain the ingestion run and its
                    # failing stage. The lightweight similarity preview is
                    # intentionally read-only and therefore has no run record.
                    self.ingestion_pipeline.ingest_policy(
                        content=content,
                        filename=filename,
                        title=title,
                        version=version,
                        level_code=level_code,
                        category_code=category_code,
                    )
                raise
            if preview.get("status") == "exact_duplicate":
                duplicate = dict(preview.get("exact_duplicate") or {})
                raise ProofError(
                    "policy_exact_duplicate",
                    "An identical policy already exists.",
                    status_code=409,
                    details=duplicate,
                )
            if preview.get("status") == "decision_required" and decision is None:
                raise ProofError(
                    "similarity_decision_required",
                    "A similarity decision is required before creating the policy.",
                    status_code=409,
                    details={"candidates": preview.get("candidates") or []},
                )
            result = self.ingestion_pipeline.ingest_policy(
                content=content,
                filename=filename,
                title=title,
                version=version,
                level_code=level_code,
                category_code=category_code,
            )
            if result.get("reused"):
                policy = dict(result.get("policy") or {})
                raise ProofError(
                    "policy_exact_duplicate",
                    "An identical policy already exists.",
                    status_code=409,
                    details={
                        "policy_id": policy.get("id"),
                        "title": policy.get("title"),
                        "current_version": policy.get("version"),
                        "policy_status": policy.get("status"),
                    },
                )
            created_policy_id = str((result.get("policy") or {}).get("id") or "") or None
            result = self._finalize_ingestion_result(
                result,
                decision=decision,
                candidate_policy_id=candidate_id,
                idempotency_key=normalized_key,
                replayed=False,
                dispatch_audit=dispatch_audit,
            )
            policy = result["policy"]
            document = result["document"]
            self.repository.complete_policy_create_request(
                idempotency_key=normalized_key,
                policy_id=str(policy["id"]),
                document_id=str(document["id"]),
            )
            created_policy_id = None
            return result
        except Exception as exc:
            if created_policy_id:
                try:
                    self._discard_policy(created_policy_id)
                except Exception:
                    logger.exception(
                        "Failed to compensate an incomplete policy create: policy_id=%s",
                        created_policy_id,
                    )
            self.repository.fail_policy_create_request(
                idempotency_key=normalized_key,
                error_code=getattr(exc, "code", "policy_create_failed"),
                error_message=str(exc),
            )
            raise

    def _finalize_ingestion_result(
        self,
        result: dict[str, Any],
        *,
        decision: str | None,
        candidate_policy_id: str | None,
        idempotency_key: str,
        replayed: bool,
        dispatch_audit: bool,
    ) -> dict[str, Any]:
        result = dict(result)
        policy = dict(result["policy"])
        document = dict(result["document"])
        similarity = result.get("similarity")
        if not isinstance(similarity, dict):
            similarity = dict(policy.get("similarity_report") or {})
        similarity.setdefault("status", policy.get("similarity_state") or "clear")
        if similarity["status"] == "decision_required":
            if decision is None:
                raise ProofError(
                    "similarity_decision_required",
                    "A similarity decision is required before creating the policy.",
                    status_code=409,
                    details={"candidates": similarity.get("candidates") or []},
                )
            policy = self.repository.decide_policy_similarity(
                policy["id"],
                decision=decision,
                candidate_policy_id=candidate_policy_id,
                idempotency_key=idempotency_key,
            )
            if policy is None:
                raise ProofError("policy_not_found", "Policy not found.", status_code=404)
            similarity = {
                **dict(policy.get("similarity_report") or {}),
                "status": policy.get("similarity_state") or decision,
                "decision": decision,
            }
        elif not replayed:
            allocator = getattr(self.repository, "resolve_independent_policy_identity", None)
            if callable(allocator):
                preview_status = str(similarity.get("status") or "clear")
                policy = allocator(policy["id"])
                similarity = {
                    **similarity,
                    "status": preview_status,
                    "decision": "separate",
                }
        result["policy"] = policy
        result["document"] = document
        result["similarity"] = similarity
        result["idempotency_replay"] = replayed
        audit_state = (
            self.policy_audit_service.ensure_dispatched(document["id"])
            if dispatch_audit
            else self.policy_audit_service.get_state(document["id"], reconcile=False)
        )
        result["audit_task"] = _audit_task_view(audit_state)
        return result

    def dispatch_policy_audit(
        self,
        *,
        policy_id: str | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_document_id = str(document_id or "").strip()
        if not resolved_document_id:
            resolved_policy_id = str(policy_id or "").strip()
            if not resolved_policy_id:
                raise ValueError("policy_id or document_id is required")
            policy = self.get_policy(resolved_policy_id)
            resolved_document_id = str(policy["document_id"])
        return self.policy_audit_service.ensure_dispatched(resolved_document_id)

    def preview_policy_similarity(
        self,
        *,
        content: bytes,
        filename: str,
        candidate_files: list[tuple[bytes, str]],
        title: str = "",
        category_code: str = "auto",
    ) -> dict[str, Any]:
        return self.ingestion_pipeline.preview_similarity(
            content=content,
            filename=filename,
            candidate_files=candidate_files,
            title=title,
            category_code=category_code,
        )

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
        base_url = self.settings.framework_base_url.strip().rstrip("/")
        if base_url:
            for item in items:
                run_id = str(item.get("framework_run_id") or "").strip()
                if item.get("operation_status") not in {"ACCEPTED", "RUNNING"} or not run_id:
                    continue
                try:
                    with httpx.Client(base_url=base_url, timeout=3) as client:
                        response = client.get(
                            f"/task-manager/runs/{run_id}",
                            headers={
                                "X-Tenant-ID": current_tenant_id(),
                                "X-User-ID": self.settings.framework_user_id,
                            },
                        )
                        response.raise_for_status()
                        run = response.json().get("run") or {}
                except (httpx.HTTPError, ValueError):
                    continue
                execution_state = str(run.get("execution_state") or "").upper()
                if execution_state == "WAITING_RESOURCE":
                    item["operation_status"] = "WAITING_READERS"
                elif execution_state == "RUNNING":
                    item["operation_status"] = "RUNNING"
                elif execution_state == "SUCCEEDED":
                    item["operation_status"] = "SUCCEEDED"
                elif execution_state in {"FAILED", "CANCELED", "CANCELLED"}:
                    item["operation_status"] = "FAILED"
                item["blocking_reader_count"] = int(
                    run.get("blocking_reader_count") or 0
                )
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
        if self.dataset_auditor is not None:
            return self.dataset_auditor
        if self.dataset_root is None:
            raise ProofError(
                "dataset_unconfigured",
                "PROOF_DATASET_ROOT is not configured.",
                status_code=503,
            )
        tenant_id = current_tenant_id()
        auditor = self._tenant_dataset_auditors.get(tenant_id)
        if auditor is not None:
            return auditor
        root = self.dataset_root / "tenants" / tenant_storage_key(tenant_id)
        root.mkdir(parents=True, exist_ok=True)
        auditor = DatasetAuditor(root, max_upload_bytes=self.settings.max_upload_bytes)
        self._tenant_dataset_auditors[tenant_id] = auditor
        return auditor

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

    def get_review_status(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        operation = self.repository.get_latest_policy_operation(policy_id)
        operation_view = self._policy_operation_view(operation)
        if policy.get("similarity_state") == "decision_required":
            return {
                "id": None,
                "policy_id": policy_id,
                "framework_task_id": None,
                "framework_run_id": None,
                "status": "not_started",
                "reason": "similarity_decision_required",
                "can_confirm": False,
                "stages": {},
                "counts": {},
                "policy_status": policy.get("status"),
                "policy_operation": operation_view,
            }
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
            "policy_status": policy.get("status"),
            "policy_operation": operation_view,
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

    def get_review_result(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        status = self.get_review_status(policy_id)
        summary = self._get_policy_summary(policy_id)
        semantic = self._get_semantic_findings(policy_id)
        conflict = self._get_conflict_findings(policy_id)
        intra_conflict = self._get_intra_conflict_findings(policy_id)
        return {
            "policy": {
                "id": policy.get("id"),
                "document_id": policy.get("document_id"),
                "family_id": policy.get("family_id"),
                "title": policy.get("title"),
                "version": policy.get("version"),
                "version_seq": policy.get("version_seq"),
                "status": policy.get("status"),
                "level_code": policy.get("level_code"),
                "category_code": policy.get("category_code"),
            },
            "review": status,
            "summary": summary,
            "findings": {
                "semantic": semantic,
                "conflict": conflict,
                "intra_conflict": intra_conflict,
            },
        }

    def _policy_operation_view(
        self,
        operation: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not operation:
            return None
        view = dict(operation)
        view["blocking_reader_count"] = 0
        run_id = str(operation.get("framework_run_id") or "").strip()
        base_url = self.settings.framework_base_url.strip().rstrip("/")
        if operation.get("status") not in {"ACCEPTED", "RUNNING"} or not run_id or not base_url:
            return view
        try:
            with httpx.Client(base_url=base_url, timeout=3) as client:
                response = client.get(
                    f"/task-manager/runs/{run_id}",
                    headers={
                        "X-Tenant-ID": current_tenant_id(),
                        "X-User-ID": self.settings.framework_user_id,
                    },
                )
                response.raise_for_status()
                run = response.json().get("run") or {}
        except (httpx.HTTPError, ValueError):
            return view
        state = str(run.get("execution_state") or "").upper()
        if state == "WAITING_RESOURCE":
            view["status"] = "WAITING_READERS"
        elif state == "RUNNING":
            view["status"] = "RUNNING"
        elif state in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}:
            error_message = str(run.get("error_message") or "").strip()
            if state == "SUCCEEDED" and not error_message:
                error_message = "Framework completed without committing the policy operation."
            elif not error_message:
                error_message = f"Framework policy operation ended with {state}."
            self.repository.complete_policy_operation(
                str(operation["operation_id"]),
                status="FAILED",
                error_message=error_message[:2000],
            )
            view["status"] = "FAILED"
            view["error_message"] = error_message
        view["blocking_reader_count"] = int(run.get("blocking_reader_count") or 0)
        return view

    def _get_policy_summary(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        return self._summary_state_for_policy(policy, policy["document_id"])

    def _get_semantic_findings(self, policy_id: str) -> dict[str, Any]:
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

    def _get_conflict_findings(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        document_id = policy["document_id"]
        audit = self._conflict_state_for_policy(policy, document_id)
        findings = self.policy_audit_service.conflict_findings(document_id)
        return {
            **_stage_status(audit),
            "findings": self._with_conflict_candidate_availability(findings),
        }

    def _get_intra_conflict_findings(self, policy_id: str) -> dict[str, Any]:
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

    def _validate_policy_activation(
        self,
        policy_id: str,
        *,
        replace_existing: bool,
    ) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        if policy.get("status") == "effective":
            return policy
        if policy.get("status") not in {"draft", "expired"}:
            raise ProofError(
                "policy_not_activatable",
                "Only a draft or expired policy can be activated.",
                status_code=409,
            )
        if policy.get("status") == "draft" and self.settings.semantic_audit_enabled:
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
        effective_lookup = getattr(self.repository, "get_effective_family_policy", None)
        effective_policy = effective_lookup(policy_id) if callable(effective_lookup) else None
        if (
            effective_policy
            and int(effective_policy.get("version_seq") or 0) > int(policy.get("version_seq") or 0)
            and not replace_existing
        ):
            raise ProofError(
                "higher_version_effective",
                "A higher policy version is already effective.",
                status_code=409,
                details={
                    "current_policy_id": str(effective_policy.get("id") or ""),
                    "current_title": str(effective_policy.get("title") or ""),
                    "current_version": str(effective_policy.get("version") or ""),
                    "target_policy_id": policy_id,
                    "target_version": str(policy.get("version") or ""),
                },
            )
        return policy

    def execute_policy_action(
        self,
        policy_id: str,
        *,
        action: str,
        idempotency_key: str | None,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in POLICY_LIFECYCLE_ACTIONS:
            raise ProofError(
                "invalid_policy_action",
                "Policy action must be activate, expire, discard, or delete.",
                status_code=422,
            )
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            raise ProofError(
                "invalid_idempotency_key",
                "Idempotency-Key is required for policy lifecycle operations.",
                status_code=422,
            )
        if len(normalized_key) > 128:
            raise ProofError(
                "invalid_idempotency_key",
                "Idempotency-Key must not exceed 128 characters.",
                status_code=422,
            )
        operation_seed = (
            f"{current_tenant_id()}:{policy_id}:{normalized_action}:"
            f"{normalized_key}:{bool(replace_existing)}"
        )
        operation_id = hashlib.sha256(operation_seed.encode("utf-8")).hexdigest()
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "policy_id": policy_id,
                    "action": normalized_action,
                    "replace_existing": bool(replace_existing),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        find_by_key = getattr(
            self.repository,
            "get_policy_operation_by_idempotency_key",
            None,
        )
        existing_operation = (
            find_by_key(normalized_key)
            if callable(find_by_key)
            else self.repository.get_policy_operation(operation_id)
        )
        if existing_operation is None:
            existing_operation = self.repository.get_policy_operation(operation_id)
        if existing_operation is not None:
            if (
                str(existing_operation.get("policy_id") or "") != policy_id
                or str(existing_operation.get("action") or "") != normalized_action
                or (
                    existing_operation.get("request_fingerprint")
                    and str(existing_operation["request_fingerprint"])
                    != request_fingerprint
                )
            ):
                raise ProofError(
                    "idempotency_conflict",
                    "The lifecycle operation was already used with different parameters.",
                    status_code=409,
                )
            operation_id = str(existing_operation.get("operation_id") or operation_id)
            if str(existing_operation.get("status") or "") != "FAILED":
                return self._policy_operation_response(
                    policy_id,
                    normalized_action,
                    {"operation_id": operation_id, **existing_operation},
                )
        policy = self.repository.get_policy(policy_id)
        if policy is None:
            tombstone = (
                self.repository.get_delete_tombstone(operation_id)
                if normalized_action == "delete"
                else None
            )
            if tombstone is None:
                raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        if normalized_action == "activate" and policy is not None:
            self._validate_policy_activation(
                policy_id,
                replace_existing=bool(replace_existing),
            )
        if (
            normalized_action == "activate"
            and policy is not None
            and policy.get("status") not in {"draft", "expired"}
        ):
            if policy.get("status") == "effective":
                return {
                    "id": policy_id,
                    "policy_id": policy_id,
                    "status": "effective",
                    "policy_status": "effective",
                    "operation": "activate",
                    "operation_status": "SUCCEEDED",
                }
            raise ProofError(
                "policy_not_activatable",
                "Only a draft or expired policy can be activated.",
                status_code=409,
            )
        if (
            normalized_action in {"expire", "delete"}
            and policy is not None
            and policy.get("status") == "draft"
        ):
            raise ProofError(
                "policy_not_published",
                "Draft policies must use the draft discard operation.",
                status_code=409,
            )
        if (
            normalized_action == "discard"
            and policy is not None
            and policy.get("status") != "draft"
        ):
            raise ProofError(
                "policy_not_draft",
                "Only draft policies can be discarded.",
                status_code=409,
            )
        claimed, claimed_operation = self.repository.claim_policy_operation(
            operation_id=operation_id,
            policy_id=policy_id,
            action=normalized_action,
            idempotency_key=normalized_key,
            request_fingerprint=request_fingerprint,
        )
        if not claimed:
            return self._policy_operation_response(
                policy_id,
                normalized_action,
                claimed_operation,
            )
        operation_id = str(claimed_operation.get("operation_id") or operation_id)
        attempt_count = int(claimed_operation.get("attempt_count") or 1)
        if normalized_action == "discard":
            self.repository.record_policy_operation(
                operation_id=operation_id,
                policy_id=policy_id,
                action=normalized_action,
                status="RUNNING",
            )
            try:
                self._discard_policy(policy_id)
            except Exception as exc:
                self.repository.complete_policy_operation(
                    operation_id,
                    status="FAILED",
                    error_message=str(exc)[:2000],
                )
                raise
            self.repository.complete_policy_operation(operation_id, status="SUCCEEDED")
            return {
                "id": policy_id,
                "policy_id": policy_id,
                "status": "discarded",
                "policy_status": "discarded",
                "operation_id": operation_id,
                "operation": normalized_action,
                "operation_status": "SUCCEEDED",
                "framework_task_id": None,
                "framework_run_id": None,
                "blocking_reader_count": 0,
            }
        try:
            return self._dispatch_policy_action(
                policy_id=policy_id,
                action=normalized_action,
                operation_id=operation_id,
                idempotency_key=normalized_key,
                attempt_count=attempt_count,
                replace_existing=bool(replace_existing),
            )
        except Exception as exc:
            self.repository.complete_policy_operation(
                operation_id,
                status="FAILED",
                error_message=str(exc)[:2000],
            )
            raise

    def _policy_operation_response(
        self,
        policy_id: str,
        action: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.repository.get_policy(policy_id)
        policy_status = (
            str(current.get("status"))
            if current is not None
            else "discarded" if action == "discard" else "deleted"
        )
        operation_view = self._policy_operation_view(operation) or operation
        operation_status = str(operation_view.get("status") or "").upper()
        response_status = (
            "accepted"
            if operation_status in {"ACCEPTED", "RUNNING", "WAITING_READERS"}
            else policy_status
        )
        return {
            "id": policy_id,
            "policy_id": policy_id,
            "status": response_status,
            "policy_status": policy_status,
            "operation_id": str(operation_view.get("operation_id") or ""),
            "operation": action,
            "operation_status": operation_status,
            "framework_task_id": operation_view.get("framework_task_id"),
            "framework_run_id": operation_view.get("framework_run_id"),
            "blocking_reader_count": int(
                operation_view.get("blocking_reader_count") or 0
            ),
            "error_message": operation_view.get("error_message"),
        }

    def _dispatch_policy_action(
        self,
        *,
        policy_id: str,
        action: str,
        operation_id: str,
        idempotency_key: str,
        attempt_count: int = 1,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        base_url = self.settings.framework_base_url.strip().rstrip("/")
        if not base_url:
            raise ProofError(
                "framework_not_configured",
                "Framework is required for policy lifecycle operations.",
                status_code=503,
            )
        headers = {
            "X-Tenant-ID": current_tenant_id(),
            "X-User-ID": self.settings.framework_user_id,
        }
        create_payload = {
            "task_type": "proof.policy.mutate",
            "title": f"Proof policy {action}",
            "model_pack_id": self.model_runtime.pack_id,
            "input_payload": {
                "operation_id": operation_id,
                "policy_id": policy_id,
                "action": action,
                "replace_existing": replace_existing,
            },
            "stream": False,
        }
        try:
            with httpx.Client(base_url=base_url, timeout=20) as client:
                response = client.post(
                    "/task-manager/tasks",
                    json=create_payload,
                    headers={
                        **headers,
                        "Idempotency-Key": f"proof-policy-action:{idempotency_key}",
                    },
                )
                response.raise_for_status()
                task_id = str(response.json()["task"]["id"])
                response = client.post(
                    f"/task-manager/tasks/{task_id}/runs",
                    json={"stream": False},
                    headers={
                        **headers,
                        "Idempotency-Key": (
                            f"proof-policy-action-run:{idempotency_key}:{attempt_count}"
                        ),
                    },
                )
                response.raise_for_status()
                run_id = str(response.json()["run_id"])
        except httpx.HTTPError as exc:
            raise ProofError(
                "framework_dispatch_failed",
                "Unable to enqueue the policy lifecycle operation.",
                status_code=503,
                details={"reason": str(exc)[:1000]},
            ) from exc
        self.repository.record_policy_operation(
            operation_id=operation_id,
            policy_id=policy_id,
            action=action,
            status="ACCEPTED",
            framework_task_id=task_id,
            framework_run_id=run_id,
        )
        return {
            "id": policy_id,
            "policy_id": policy_id,
            "status": "accepted",
            "policy_status": str(
                (self.repository.get_policy(policy_id) or {}).get("status") or ""
            ),
            "operation_id": operation_id,
            "operation": action,
            "operation_status": "ACCEPTED",
            "framework_task_id": task_id,
            "framework_run_id": run_id,
        }

    def apply_policy_action(
        self,
        *,
        policy_id: str,
        action: str,
        operation_id: str,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        self.repository.record_policy_operation(
            operation_id=operation_id,
            policy_id=policy_id,
            action=action,
            status="RUNNING",
        )
        try:
            if action == "activate":
                model_runtime = getattr(self, "model_runtime", None)
                if model_runtime is None or not model_runtime.embedding_configured:
                    raise ProofError(
                        "embedding_not_configured",
                        "An embedding model is required before a policy can become effective.",
                        status_code=503,
                    )
                current_policy = self.repository.get_policy(policy_id)
                if current_policy is None:
                    raise ProofError("policy_not_found", "Policy not found.", status_code=404)
                index_result = self._index_document(
                    current_policy["document_id"],
                    require_effective=False,
                )
                if (
                    index_result.get("status") != "indexed"
                    or int(index_result.get("indexed_unit_count") or 0) <= 0
                ):
                    raise ProofError(
                        "embedding_incomplete",
                        "Policy indexing did not produce any searchable vectors.",
                        status_code=502,
                    )
                policy = self.repository.activate_policy_version(
                    policy_id,
                    replace_existing=replace_existing,
                )
                if policy is None:
                    raise ProofError("policy_not_found", "Policy not found.", status_code=404)
                result = {
                    "id": policy_id,
                    "status": str(policy["status"]),
                    "operation_id": operation_id,
                }
            elif action == "expire":
                policy = self.repository.expire_policy_version(policy_id)
                if policy is None:
                    raise ProofError("policy_not_found", "Policy not found.", status_code=404)
                result = {
                    "id": policy_id,
                    "status": str(policy["status"]),
                    "operation_id": operation_id,
                }
            elif action == "delete":
                result = self._delete_policy_version(policy_id, operation_id=operation_id)
            else:
                raise ProofError(
                    "invalid_policy_action",
                    "Unknown policy lifecycle action.",
                    status_code=422,
                )
        except Exception as exc:
            self.repository.complete_policy_operation(
                operation_id,
                status="FAILED",
                error_message=str(exc)[:2000],
            )
            raise
        self.repository.complete_policy_operation(operation_id, status="SUCCEEDED")
        return result

    def _delete_policy_version(self, policy_id: str, *, operation_id: str) -> dict[str, Any]:
        tombstone = self.repository.get_delete_tombstone(operation_id)
        policy = self.repository.get_policy(policy_id)
        if tombstone is None and policy is None:
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        original_relative = str(
            (tombstone or {}).get("original_storage_path")
            or (policy or {}).get("storage_path")
            or ""
        )
        filename = Path(original_relative).name or f"{policy_id}.bin"
        trash_relative = str(
            (tombstone or {}).get("trash_storage_path")
            or (
                Path(".trash")
                / tenant_storage_key()
                / operation_id
                / filename
            )
        )
        original_path = (self.storage_root / original_relative).resolve()
        trash_path = (self.storage_root / trash_relative).resolve()
        if not original_path.is_relative_to(self.storage_root) or not trash_path.is_relative_to(
            self.storage_root
        ):
            raise ProofError("invalid_storage_path", "Policy storage path is invalid.", status_code=500)
        deleted = self.repository.delete_policy_version(
            policy_id,
            operation_id=operation_id,
            original_storage_path=original_relative,
            trash_storage_path=trash_relative,
        )
        if deleted is None:
            raise ProofError("policy_not_found", "Policy not found.", status_code=404)
        try:
            trash_path.parent.mkdir(parents=True, exist_ok=True)
            if original_path.is_file() and not trash_path.exists():
                original_path.replace(trash_path)
            if trash_path.is_file():
                trash_path.unlink()
            self.repository.mark_delete_cleaned(operation_id)
        except OSError as exc:
            self.repository.mark_delete_cleanup_failed(operation_id, str(exc))
            raise ProofError(
                "file_cleanup_failed",
                "Policy data was removed but physical file cleanup must be retried.",
                status_code=503,
            ) from exc
        return {"id": policy_id, "status": "deleted", "operation_id": operation_id}

    def _discard_policy(self, policy_id: str) -> dict[str, Any]:
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

    def _index_document(
        self,
        document_id: str,
        *,
        require_effective: bool,
    ) -> dict[str, Any]:
        policy = self.repository.get_policy_by_document_id(document_id)
        if policy is None:
            raise ProofError("document_not_found", "Document or clauses not found.", status_code=404)
        allowed_statuses = (
            {"effective"}
            if require_effective
            else {"draft", "effective", "expired"}
        )
        if policy.get("status") not in allowed_statuses:
            raise ProofError(
                "policy_not_effective",
                "The policy cannot be indexed in its current state.",
                status_code=409,
            )
        client = OpenAICompatibleEmbeddingClient(self.model_runtime.embedding)
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
            conflict_reranker_config = self.model_runtime.reranker
            self.conflict_retrieval_service = ConflictRetrievalService(
                repository=self.repository,
                embedding_client=OpenAICompatibleEmbeddingClient(
                    self.model_runtime.embedding
                ),
                reranker=(
                    DashScopePolicyReranker(
                        conflict_reranker_config,
                        instruction=CONFLICT_RERANK_INSTRUCTION,
                    )
                    if conflict_reranker_config is not None
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


def _not_started_audit_task() -> dict[str, Any]:
    return {
        "id": None,
        "framework_task_id": None,
        "framework_run_id": None,
        "status": "not_started",
        "reason": "similarity_decision_required",
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
