"""Test-only API for exercising one Contract IR Window with the real Framework LLM."""

from __future__ import annotations

import os
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from services.contract.capabilities.window_extraction import (
    WindowExtractionEngine,
    WindowExtractionError,
    WindowExtractionRequest,
    WindowExtractionResult,
)
from services.contract.capabilities.window_pipeline import (
    ContractIrWindowPipeline,
    WindowPipelineError,
    WindowPipelineRequest,
    WindowPipelineResult,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractionTestRequest(StrictModel):
    window: WindowExtractionRequest
    tenant_id: str | None = None
    model_id: str | None = None


class ExtractionTestResponse(StrictModel):
    duration_ms: int
    result: WindowExtractionResult


class PipelineTestRequest(StrictModel):
    pipeline: WindowPipelineRequest
    tenant_id: str | None = None
    model_id: str | None = None


def create_app(engine: WindowExtractionEngine | None = None) -> FastAPI:
    app = FastAPI(
        title="Contract IR Window Extractor Test API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
    )
    extractor = engine or WindowExtractionEngine()

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "UP"}

    @app.post("/api/extract-window", response_model=ExtractionTestResponse)
    async def extract_window(payload: ExtractionTestRequest):
        tenant_id = payload.tenant_id or os.getenv("CONTRACT_TEST_TENANT_ID", "default")
        model_id = payload.model_id or os.getenv("CONTRACT_MODEL_ID", "")
        if not model_id:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "MODEL_ID_REQUIRED",
                        "message": "CONTRACT_MODEL_ID is not configured",
                    }
                },
            )
        started = time.perf_counter()
        try:
            result = await extractor.extract(
                payload.window,
                tenant_id=tenant_id,
                model_id=model_id,
            )
        except WindowExtractionError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": exc.code, "message": str(exc)}},
            )
        return ExtractionTestResponse(
            duration_ms=round((time.perf_counter() - started) * 1000),
            result=result,
        )

    @app.post("/api/extract-all", response_model=WindowPipelineResult)
    async def extract_all(payload: PipelineTestRequest):
        tenant_id = payload.tenant_id or os.getenv("CONTRACT_TEST_TENANT_ID", "default")
        model_id = payload.model_id or os.getenv("CONTRACT_MODEL_ID", "")
        if not model_id:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "MODEL_ID_REQUIRED",
                        "message": "CONTRACT_MODEL_ID is not configured",
                    }
                },
            )
        pipeline = ContractIrWindowPipeline(extractor=extractor)
        try:
            return await pipeline.run(
                payload.pipeline,
                tenant_id=tenant_id,
                model_id=model_id,
            )
        except WindowPipelineError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                    }
                },
            )

    return app


app = create_app()
