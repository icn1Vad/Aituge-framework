from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from proof.api.schemas import (
    ConflictRetrievalRequest,
    IntraConflictRetrievalRequest,
    PolicySqlRequest,
    RetrievalFetchRequest,
    RetrievalSearchRequest,
    SimilarityDecisionRequest,
    InternalPolicyActionRequest,
    PolicyLifecycleActionRequest,
)
from proof.application.service import ProofService
from proof.config import Settings, get_settings
from proof.errors import ProofError
from proof.model_pack import (
    AI_MODE_HEADER,
    MODEL_PACK_ID_HEADER,
    current_model_pack_id,
    model_pack_scope,
    resolve_ai_mode_model_pack_id,
    resolve_model_pack_id,
)
from proof.model_runtime import build_proof_model_runtime
from proof.tenant import TENANT_ID_HEADER, normalize_tenant_id, tenant_scope

DATASET_PAGE = Path(__file__).with_name("static") / "dataset.html"
WORKBENCH_PAGE = Path(__file__).with_name("static") / "workbench.html"
EXAMPLE_POLICY = Path(__file__).parents[3] / "examples" / "采购管理制度（试行）.txt"
POLICY_LEVEL_HIERARCHY = (
    {"code": "upper", "name": "一级制度", "rank": 300},
    {"code": "peer", "name": "二级制度", "rank": 200},
    {"code": "lower", "name": "三级制度", "rank": 100},
)
POLICY_LEVEL_BY_CODE = {item["code"]: item for item in POLICY_LEVEL_HIERARCHY}


def _policy_view(value: dict) -> dict:
    """Expose one canonical metadata shape at the Java-facing API boundary."""

    policy = dict(value)
    level_code = policy.pop("level_code", None)
    level_name = policy.pop("level_name", None)
    category_code = policy.pop("category_code", None)
    category_name = policy.pop("category_name", None)

    level = policy.get("level")
    if not isinstance(level, dict):
        registered_level = POLICY_LEVEL_BY_CODE.get(str(level_code or ""), {})
        policy["level"] = (
            {
                "code": level_code,
                "name": level_name or registered_level.get("name"),
                "sort_rank": registered_level.get("rank"),
            }
            if level_code or level_name
            else None
        )

    category = policy.get("category")
    if not isinstance(category, dict):
        policy["category"] = (
            {
                "code": category_code,
                "name": category_name,
                "path_name": category_name,
            }
            if category_code or category_name
            else None
        )
    return policy


def _policy_ingestion_view(value: dict) -> dict:
    payload = dict(value)
    policy = payload.get("policy")
    if isinstance(policy, dict):
        payload["policy"] = _policy_view(policy)
    return payload


def create_app(settings: Settings | None = None, service: ProofService | None = None) -> FastAPI:
    app = FastAPI(title="Proof Service", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.state.proof_service = service
    app.state.proof_services = {}

    @app.middleware("http")
    async def bind_tenant_context(request: Request, call_next):
        if not request.url.path.startswith("/v1"):
            return await call_next(request)
        raw_tenant_id = request.headers.get(TENANT_ID_HEADER)
        try:
            tenant_id = normalize_tenant_id(raw_tenant_id)
            mode_pack_id = resolve_ai_mode_model_pack_id(
                request.app.state.settings,
                request.headers.get(AI_MODE_HEADER),
            )
            model_pack_id = resolve_model_pack_id(
                request.app.state.settings,
                mode_pack_id or request.headers.get(MODEL_PACK_ID_HEADER),
            )
        except ProofError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "success": False,
                    "error": exc.code,
                    "detail": str(exc),
                    "details": exc.details,
                },
            )
        with tenant_scope(tenant_id), model_pack_scope(model_pack_id):
            return await call_next(request)

    @app.exception_handler(ProofError)
    async def handle_proof_error(request: Request, exc: ProofError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": exc.code,
                "detail": str(exc),
                "details": exc.details,
            },
        )

    @app.get("/health")
    async def health(request: Request):
        data = await asyncio.to_thread(_service(request).health)
        return {"success": True, "data": data}

    @app.get("/dataset", response_class=HTMLResponse, include_in_schema=False)
    async def dataset_page():
        return HTMLResponse(DATASET_PAGE.read_text("utf-8"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/workbench", response_class=HTMLResponse, include_in_schema=False)
    async def workbench_page():
        return HTMLResponse(WORKBENCH_PAGE.read_text("utf-8"))

    @app.get("/examples/policy-semantic-conflict-test.txt", include_in_schema=False)
    async def example_policy_file():
        return FileResponse(
            EXAMPLE_POLICY,
            media_type="text/plain; charset=utf-8",
            filename="采购管理制度（试行）.txt",
        )

    @app.post("/v1/policies")
    async def create_policy(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        title: Annotated[str, Form()] = "",
        version: Annotated[str, Form()] = "v1.0.0",
        level_code: Annotated[str, Form()] = "",
        category_code: Annotated[str, Form()] = "auto",
    ):
        settings = request.app.state.settings
        content = await file.read(settings.max_upload_bytes + 1)
        data = await asyncio.to_thread(
            _service(request).ingest_policy,
            content=content,
            filename=file.filename or "policy.txt",
            title=title,
            version=version,
            level_code=level_code,
            category_code=category_code,
        )
        return {"success": True, "data": _policy_ingestion_view(data)}

    @app.get("/v1/policies")
    async def list_policies(
        request: Request,
        level_code: str | None = None,
        category_code: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        data = await asyncio.to_thread(
            _service(request).list_policies,
            level_code=level_code,
            category_code=category_code,
            limit=limit,
            offset=offset,
        )
        return {"success": True, "data": [_policy_view(item) for item in data]}

    @app.get("/v1/policies/{policy_id}")
    async def get_policy(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_policy, policy_id)
        return {"success": True, "data": _policy_view(data)}

    @app.get("/v1/policies/{policy_id}/clauses")
    async def list_clauses(policy_id: str, request: Request, include_text: bool = False):
        data = await asyncio.to_thread(_service(request).list_clauses, policy_id, include_text=include_text)
        return {"success": True, "data": data}

    @app.get("/v1/files")
    async def list_files(request: Request):
        data = await asyncio.to_thread(_service(request).list_files)
        return {"success": True, "data": data}

    @app.get("/v1/files/{file_id}/content")
    async def get_file_content(file_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_file_content, file_id)
        return FileResponse(
            data["path"],
            media_type=data["media_type"],
            filename=data["name"],
            content_disposition_type="inline",
        )

    @app.get("/v1/files/{file_id}/chunks")
    async def list_file_chunks(
        file_id: str,
        request: Request,
        limit: int = Query(default=10, ge=1, le=10),
        offset: int = Query(default=0, ge=0),
    ):
        data = await asyncio.to_thread(
            _service(request).list_file_chunks,
            file_id,
            limit=limit,
            offset=offset,
        )
        return {"success": True, "data": data}

    @app.get("/v1/policies/{policy_id}/audit-status")
    async def get_audit_status(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_audit_status, policy_id)
        return {"success": True, "data": data}

    @app.post("/v1/policies/{policy_id}/similarity-decision")
    async def decide_policy_similarity(
        policy_id: str,
        payload: SimilarityDecisionRequest,
        request: Request,
    ):
        data = await asyncio.to_thread(
            _service(request).decide_policy_similarity,
            policy_id,
            decision=payload.decision,
            candidate_policy_id=payload.candidate_policy_id,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return {"success": True, "data": _policy_ingestion_view(data)}

    @app.get("/v1/policies/{policy_id}/policy-summary")
    async def get_policy_summary(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_policy_summary, policy_id)
        return {"success": True, "data": data}

    @app.get("/v1/policies/{policy_id}/semantic-findings")
    async def get_semantic_findings(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_semantic_findings, policy_id)
        return {"success": True, "data": data}

    @app.get("/v1/policies/{policy_id}/conflict-findings")
    async def get_conflict_findings(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_conflict_findings, policy_id)
        return {"success": True, "data": data}

    @app.get("/v1/policies/{policy_id}/intra-conflict-findings")
    async def get_intra_conflict_findings(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_intra_conflict_findings, policy_id)
        return {"success": True, "data": data}

    @app.post("/v1/policies/{policy_id}/confirm")
    async def confirm_policy(policy_id: str, request: Request):
        data = await asyncio.to_thread(
            _service(request).confirm_policy,
            policy_id,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return {"success": True, "data": data}

    @app.post("/v1/policies/{policy_id}/actions")
    async def request_policy_action(
        policy_id: str,
        payload: PolicyLifecycleActionRequest,
        request: Request,
    ):
        data = await asyncio.to_thread(
            _service(request).request_policy_action,
            policy_id,
            action=payload.action,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return {"success": True, "data": data}

    @app.delete("/v1/policies/{policy_id}")
    async def discard_policy(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).discard_policy, policy_id)
        return {"success": True, "data": data}

    @app.post("/v1/internal/semantic-audits/result", include_in_schema=False)
    async def semantic_audit_result(request: Request, payload: dict = Body(...)):
        data = await asyncio.to_thread(_service(request).accept_semantic_audit_result, payload)
        return {"success": True, "data": data}

    @app.post("/v1/internal/policy-actions/apply", include_in_schema=False)
    async def apply_policy_action(
        payload: InternalPolicyActionRequest,
        request: Request,
    ):
        data = await asyncio.to_thread(
            _service(request).apply_policy_action,
            policy_id=payload.policy_id,
            action=payload.action,
            operation_id=payload.operation_id,
        )
        return {"success": True, "data": data}

    @app.post("/v1/internal/conflict-audits/result", include_in_schema=False)
    async def conflict_audit_result(request: Request, payload: dict = Body(...)):
        data = await asyncio.to_thread(_service(request).accept_conflict_audit_result, payload)
        return {"success": True, "data": data}

    @app.get("/v1/ingestion-runs/{run_id}")
    async def get_ingestion_run(run_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_ingestion_run, run_id)
        return {"success": True, "data": data}

    @app.get("/v1/dataset/audit")
    async def audit_dataset(request: Request, refresh: bool = False):
        data = await asyncio.to_thread(_service(request).audit_dataset, refresh=refresh)
        return {"success": True, "data": data}

    @app.get("/v1/dataset/files/{file_id}")
    async def get_dataset_file(file_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_dataset_file, file_id)
        return {"success": True, "data": data}

    @app.get("/v1/categories/levels")
    async def list_levels(request: Request):
        return {"success": True, "data": await asyncio.to_thread(_service(request).list_levels)}

    @app.get("/v1/categories/policies")
    async def list_categories(request: Request):
        return {"success": True, "data": await asyncio.to_thread(_service(request).list_categories)}

    @app.post("/v1/documents/{document_id}/index")
    async def index_document(document_id: str, request: Request):
        return {
            "success": True,
            "data": await asyncio.to_thread(_service(request).index_document, document_id),
        }

    @app.post("/v1/retrieval/search")
    async def search(payload: RetrievalSearchRequest, request: Request):
        data = await asyncio.to_thread(
            _service(request).search,
            query=payload.query,
            top_k=payload.top_k,
            retrieval_mode=payload.retrieval_mode,
            policy_ids=payload.policy_ids,
            level_codes=payload.level_codes,
            category_codes=payload.category_codes,
        )
        return {"success": True, "data": data}

    @app.post("/v1/retrieval/fetch")
    async def fetch(payload: RetrievalFetchRequest, request: Request):
        data = await asyncio.to_thread(_service(request).fetch_units, payload.unit_ids)
        return {"success": True, "data": data}

    @app.post("/v1/internal/conflict-retrieval", include_in_schema=False)
    async def retrieve_conflict_candidates(payload: ConflictRetrievalRequest, request: Request):
        data = await asyncio.to_thread(
            _service(request).retrieve_conflict_candidates,
            payload.unit_id,
            top_k=payload.top_k,
        )
        return {"success": True, "data": _conflict_agent_view(data)}

    @app.post("/v1/internal/intra-conflict-retrieval", include_in_schema=False)
    async def retrieve_intra_conflict_candidates(
        payload: IntraConflictRetrievalRequest, request: Request
    ):
        data = await asyncio.to_thread(
            _service(request).retrieve_intra_conflict_candidates,
            payload.unit_id,
        )
        return {"success": True, "data": _intra_conflict_agent_view(data)}

    @app.post("/v1/query/sql")
    async def execute_sql(payload: PolicySqlRequest, request: Request):
        data = await asyncio.to_thread(
            _service(request).execute_sql,
            question=payload.question,
            sql=payload.sql,
        )
        return {"success": True, "data": data}

    return app


def _service(request: Request) -> ProofService:
    service = request.app.state.proof_service
    if service is not None:
        return service
    settings = request.app.state.settings
    model_pack_id = current_model_pack_id() or resolve_model_pack_id(settings)
    cache_key = model_pack_id or "__legacy__"
    cached = request.app.state.proof_services.get(cache_key)
    if cached is None:
        cached = ProofService(
            settings,
            model_runtime=build_proof_model_runtime(
                settings,
                model_pack_id=model_pack_id or None,
            ),
        )
        request.app.state.proof_services[cache_key] = cached
    return cached


def _intra_conflict_agent_view(data: dict) -> dict:
    """Expose short refs to the model while keeping real Chunk IDs server-side."""

    source = data.get("source") or {}
    results = list(data.get("results") or [])
    return {
        "source": {
            "text": source.get("text"),
            "clause_no_raw": source.get("clause_no_raw"),
            "clause_ordinal": source.get("clause_ordinal"),
        },
        "results": [
            {
                "ref": item.get("ref"),
                "text": item.get("text"),
                "clause_no_raw": item.get("clause_no_raw"),
                "clause_ordinal": item.get("clause_ordinal"),
            }
            for item in results
        ],
    }


def _conflict_agent_view(data: dict, *, limit: int | None = None) -> dict:
    """Give the Judge a compact view without changing the retrieval service's order."""

    source = data.get("source") or {}
    results = list(data.get("results") or [])

    def compact(item: dict, *, result: bool = False) -> dict:
        level = POLICY_LEVEL_BY_CODE.get(str(item.get("level_code") or ""), {})
        payload = {
            "text": item.get("text"),
            "policy_title": item.get("policy_title"),
            "policy_version": item.get("policy_version"),
            "level_code": item.get("level_code"),
            "level_name": item.get("level_name") or level.get("name"),
            "level_rank": item.get("level_rank") or level.get("rank"),
            "category_code": item.get("category_code"),
            "clause_no_raw": item.get("clause_no_raw"),
            "clause_ordinal": item.get("clause_ordinal"),
            "citation": {"label": (item.get("citation") or {}).get("label")},
        }
        if result:
            payload.update(
                {
                    "ref": item.get("ref"),
                    "retrieval_sources": item.get("retrieval_sources") or [],
                    "branch_ranks": item.get("branch_ranks") or {},
                }
            )
            if item.get("rerank_rank") is not None:
                payload["rerank_rank"] = item["rerank_rank"]
                payload["rerank_score"] = item.get("rerank_score")
        else:
            payload.update(
                {
                    "unit_id_corrected": bool(item.get("unit_id_corrected")),
                }
            )
        return payload

    compact_source = compact(source)
    compact_results = [compact(item, result=True) for item in results]
    source_rank = compact_source.get("level_rank")
    for item in compact_results:
        candidate_rank = item.get("level_rank")
        if not source_rank or not candidate_rank:
            item["level_relation"] = "unknown"
        elif candidate_rank > source_rank:
            item["level_relation"] = "candidate_is_higher"
        elif candidate_rank < source_rank:
            item["level_relation"] = "candidate_is_lower"
        else:
            item["level_relation"] = "same_level"
    if limit is not None:
        compact_results = compact_results[: max(1, limit)]
    candidate_counts = dict(data.get("candidate_counts") or {})
    candidate_counts["judge_returned"] = len(compact_results)
    return {
        "source": compact_source,
        "results": compact_results,
        "policy_level_hierarchy": {
            "precedence": [dict(item) for item in POLICY_LEVEL_HIERARCHY],
            "rule": "一级制度 > 二级制度 > 三级制度；跨层级冲突以上级制度为准。",
        },
        "candidate_counts": candidate_counts,
        "branch_metadata": data.get("branch_metadata") or {},
        "reranker_used": bool(data.get("reranker_used")),
        "degraded": bool(data.get("degraded")),
        "degradation_reasons": data.get("degradation_reasons") or [],
        "skipped_branches": data.get("skipped_branches") or [],
        "judge_order": "service_evidence_order",
        "judge_candidate_limit": len(compact_results),
    }


app = create_app()
