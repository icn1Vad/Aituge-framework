from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from proof.application.dataset_audit import DatasetAuditor
from proof.application.ingestion import PolicyIngestionPipeline
from proof.application.quality_report import build_policy_quality_report
from proof.application.retrieval import HybridPolicyRetriever, RetrievalFilters
from proof.application.semantic_audit import SemanticAuditService
from proof.application.sql_query import PolicySqlQueryService
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.postgres.repository import ProofRepository
from proof.infrastructure.reranking import DashScopePolicyReranker
from proof.infrastructure.retrieval import PgvectorPolicyRetriever, PostgresKeywordRetriever


logger = logging.getLogger(__name__)


class ProofService:
    def __init__(
        self,
        settings: Settings,
        repository: ProofRepository | None = None,
        ingestion_pipeline: PolicyIngestionPipeline | None = None,
        dataset_auditor: DatasetAuditor | None = None,
        retrieval_pipeline: HybridPolicyRetriever | None = None,
        sql_query_service: PolicySqlQueryService | None = None,
        semantic_audit_service: SemanticAuditService | None = None,
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
        self.semantic_audit_service = semantic_audit_service or SemanticAuditService(settings, self.repository)

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
        if policy.get("status") == "draft":
            result["semantic_audit"] = self.semantic_audit_service.ensure_dispatched(document["id"])
        else:
            result["semantic_audit"] = self._semantic_state_for_policy(policy, document["id"])
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

    def get_quality_report(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        clauses = self.repository.list_clauses(policy_id, include_text=True)
        document_id = policy["document_id"]
        return build_policy_quality_report(
            policy,
            clauses,
            semantic_audit=self._semantic_state_for_policy(policy, document_id),
            semantic_findings=self.semantic_audit_service.findings(document_id),
        )

    def _semantic_state_for_policy(self, policy: dict[str, Any], document_id: str) -> dict[str, Any]:
        state = self.semantic_audit_service.get_state(document_id)
        if policy.get("status") == "effective" and state.get("status") == "pending" and not state.get("id"):
            return {"status": "not_requested", "error_message": None}
        return state

    def retry_semantic_audit(self, policy_id: str) -> dict[str, Any]:
        policy = self.get_policy(policy_id)
        if policy.get("status") != "draft":
            raise ProofError(
                "policy_not_draft",
                "Semantic review can only be started for a draft policy.",
                status_code=409,
            )
        return self.semantic_audit_service.ensure_dispatched(policy["document_id"])

    def accept_semantic_audit_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.semantic_audit_service.accept_result(payload)

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
            audit = self.semantic_audit_service.get_state(policy["document_id"])
            if audit["status"] != "completed":
                raise ProofError(
                    "semantic_audit_incomplete",
                    "Semantic review must complete before the policy can be confirmed.",
                    status_code=409,
                    details={"semantic_audit": audit},
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
        storage_path = (self.storage_root / Path(deleted["storage_path"])).resolve()
        if storage_path.is_relative_to(self.storage_root) and storage_path.is_file():
            try:
                storage_path.unlink()
            except OSError:
                logger.warning("Draft %s was deleted but its source file could not be removed.", policy_id)
        return {"id": policy_id, "status": "discarded"}

    def list_levels(self) -> list[dict[str, Any]]:
        return self.repository.list_levels()

    def list_categories(self) -> list[dict[str, Any]]:
        return self.repository.list_categories()

    def create_category(self, code: str, name: str, description: str = "") -> dict[str, Any]:
        return self.repository.create_category(code, name, description)

    def fetch_units(self, unit_ids: list[str]) -> list[dict[str, Any]]:
        return [_with_citation(unit) for unit in self.repository.fetch_units(unit_ids)]

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
