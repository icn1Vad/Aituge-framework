"""Test-only API for exercising one Contract IR Window with the real Framework LLM."""

from __future__ import annotations

import os
import time
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from aituge_model.config import ModelRuntimeProvider

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from services.contract.capabilities.window_shadow import (
    ShadowCompareRequest,
    ShadowCompareResult,
    compare_contract_ir,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PartyContextInput(StrictModel):
    """Test-side projection of the validated ``resolve_parties`` artifact."""

    party_a_name: str = Field(min_length=1, max_length=500)
    party_b_name: str = Field(min_length=1, max_length=500)
    perspective: Literal["PARTY_A", "PARTY_B"]
    contract_type: str = Field(default="AUTO", min_length=1, max_length=100)
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"

    @model_validator(mode="after")
    def validate_parties(self) -> "PartyContextInput":
        self.party_a_name = self.party_a_name.strip()
        self.party_b_name = self.party_b_name.strip()
        self.contract_type = self.contract_type.strip().upper()
        if not self.party_a_name or not self.party_b_name:
            raise ValueError("party names must not be blank")
        if self.party_a_name == self.party_b_name:
            raise ValueError("party_a_name and party_b_name must be different")
        return self

    def resolve(self) -> "ResolvedPartyContext":
        our_party = self.party_a_name if self.perspective == "PARTY_A" else self.party_b_name
        counterparty = self.party_b_name if self.perspective == "PARTY_A" else self.party_a_name
        return ResolvedPartyContext(
            **self.model_dump(),
            our_party=our_party,
            counterparty=counterparty,
        )


class ResolvedPartyContext(PartyContextInput):
    our_party: str
    counterparty: str

    def as_context_text(self) -> str:
        return "\n".join(
            (
                "已验证合同主体（仅供理解，不得作为 extraction_text 或原文证据）：",
                f"PARTY_A_NAME={self.party_a_name}",
                f"PARTY_B_NAME={self.party_b_name}",
                f"PERSPECTIVE={self.perspective}",
                f"OUR_PARTY={self.our_party}",
                f"COUNTERPARTY={self.counterparty}",
                f"CONTRACT_TYPE={self.contract_type}",
                f"REVIEW_ATTITUDE={self.review_attitude}",
            )
        )


class ExtractionTestRequest(StrictModel):
    window: WindowExtractionRequest
    party_context: PartyContextInput | None = None
    tenant_id: str | None = None
    model_id: str | None = None


class ExtractionTestResponse(StrictModel):
    duration_ms: int
    result: WindowExtractionResult
    party_context: ResolvedPartyContext | None = None


class PipelineTestRequest(StrictModel):
    pipeline: WindowPipelineRequest
    party_context: PartyContextInput | None = None
    tenant_id: str | None = None
    model_id: str | None = None


class PipelineTestResponse(WindowPipelineResult):
    party_context: ResolvedPartyContext | None = None


def _with_party_context(
    window: WindowExtractionRequest,
    party_context: ResolvedPartyContext | None,
) -> WindowExtractionRequest:
    if party_context is None:
        return window
    context_parts = [party_context.as_context_text()]
    if window.context_text.strip():
        context_parts.append(window.context_text.strip())
    return window.model_copy(update={"context_text": "\n\n".join(context_parts)})


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
        model_id = (
            payload.model_id
            or ModelRuntimeProvider.from_environment().active_pack.llm.id
        )
        started = time.perf_counter()
        party_context = payload.party_context.resolve() if payload.party_context else None
        try:
            result = await extractor.extract(
                _with_party_context(payload.window, party_context),
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
            party_context=party_context,
        )

    @app.post("/api/extract-all", response_model=PipelineTestResponse)
    async def extract_all(payload: PipelineTestRequest):
        tenant_id = payload.tenant_id or os.getenv("CONTRACT_TEST_TENANT_ID", "default")
        model_id = (
            payload.model_id
            or ModelRuntimeProvider.from_environment().active_pack.llm.id
        )
        party_context = payload.party_context.resolve() if payload.party_context else None
        pipeline_request = payload.pipeline.model_copy(
            update={
                "windows": [
                    _with_party_context(window, party_context)
                    for window in payload.pipeline.windows
                ]
            }
        )
        pipeline = ContractIrWindowPipeline(extractor=extractor)
        try:
            result = await pipeline.run(
                pipeline_request,
                tenant_id=tenant_id,
                model_id=model_id,
            )
            return PipelineTestResponse(
                **result.model_dump(),
                party_context=party_context,
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

    @app.post("/api/shadow-compare", response_model=ShadowCompareResult)
    async def shadow_compare(payload: ShadowCompareRequest):
        return compare_contract_ir(payload)

    return app


app = create_app()
