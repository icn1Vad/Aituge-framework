from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from proof.api.schemas import CategoryCreate, PolicySqlRequest, RetrievalFetchRequest, RetrievalSearchRequest
from proof.application.service import ProofService
from proof.config import Settings, get_settings
from proof.errors import ProofError


DATASET_PAGE = Path(__file__).with_name("static") / "dataset.html"
WORKBENCH_PAGE = Path(__file__).with_name("static") / "workbench.html"
EXAMPLE_POLICY = Path(__file__).parents[3] / "examples" / "policy-structure-errors.txt"


def create_app(settings: Settings | None = None, service: ProofService | None = None) -> FastAPI:
    app = FastAPI(title="Proof Service", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.state.proof_service = service

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

    @app.get("/examples/policy-structure-errors.txt", include_in_schema=False)
    async def example_policy_file():
        return FileResponse(
            EXAMPLE_POLICY,
            media_type="text/plain; charset=utf-8",
            filename="policy-structure-errors.txt",
        )

    @app.post("/v1/policies")
    async def create_policy(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        title: Annotated[str, Form()] = "",
        version: Annotated[str, Form()] = "1.0",
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
        return {"success": True, "data": data}

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
        return {"success": True, "data": data}

    @app.get("/v1/policies/{policy_id}")
    async def get_policy(policy_id: str, request: Request):
        return {"success": True, "data": await asyncio.to_thread(_service(request).get_policy, policy_id)}

    @app.get("/v1/policies/{policy_id}/clauses")
    async def list_clauses(policy_id: str, request: Request, include_text: bool = False):
        data = await asyncio.to_thread(_service(request).list_clauses, policy_id, include_text=include_text)
        return {"success": True, "data": data}

    @app.get("/v1/policies/{policy_id}/quality-report")
    async def get_quality_report(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).get_quality_report, policy_id)
        return {"success": True, "data": data}

    @app.post("/v1/policies/{policy_id}/semantic-audit")
    async def retry_semantic_audit(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).retry_semantic_audit, policy_id)
        return {"success": True, "data": data}

    @app.post("/v1/policies/{policy_id}/confirm")
    async def confirm_policy(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).confirm_policy, policy_id)
        return {"success": True, "data": data}

    @app.delete("/v1/policies/{policy_id}")
    async def discard_policy(policy_id: str, request: Request):
        data = await asyncio.to_thread(_service(request).discard_policy, policy_id)
        return {"success": True, "data": data}

    @app.post("/v1/internal/semantic-audits/result", include_in_schema=False)
    async def semantic_audit_result(request: Request, payload: dict = Body(...)):
        data = await asyncio.to_thread(_service(request).accept_semantic_audit_result, payload)
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

    @app.get("/v1/meta/policy-levels")
    async def list_levels(request: Request):
        return {"success": True, "data": await asyncio.to_thread(_service(request).list_levels)}

    @app.get("/v1/categories")
    async def list_categories(request: Request):
        return {"success": True, "data": await asyncio.to_thread(_service(request).list_categories)}

    @app.post("/v1/categories")
    async def create_category(payload: CategoryCreate, request: Request):
        data = await asyncio.to_thread(
            _service(request).create_category,
            payload.code,
            payload.name,
            payload.description,
        )
        return {"success": True, "data": data}

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
    if service is None:
        service = ProofService(request.app.state.settings)
        request.app.state.proof_service = service
    return service


app = create_app()
