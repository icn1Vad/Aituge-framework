"""Standalone internal API for the Revision Draft MVP.

The production Contract API remains unchanged.  Java may later proxy this
internal endpoint without changing the formal review-result contract.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from aituge_model_config import ModelRuntimeProvider

from services.contract.capabilities.revision_drafts import (
    RevisionDraftError,
    RevisionDraftResponse,
    RevisionDraftService,
    RevisionReviewSource,
    JsonRevisionSourceProvider,
    LlmRevisionTextGenerator,
    default_cache,
)


def create_app(
    service: RevisionDraftService | None = None,
    *,
    internal_token: str | None = None,
    internal_auth_enabled: bool | None = None,
) -> FastAPI:
    auth_enabled = (
        internal_auth_enabled
        if internal_auth_enabled is not None
        else os.getenv("CONTRACT_INTERNAL_AUTH_ENABLED", "true").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    expected_token = (
        internal_token
        if internal_token is not None
        else os.getenv("CONTRACT_INTERNAL_TOKEN", "")
    )

    def authorize(
        internal_service: Annotated[str, Header(alias="X-Internal-Service")],
        supplied_token: Annotated[str, Header(alias="X-Internal-Token")],
        request_id: Annotated[str, Header(alias="X-Request-Id")],
    ) -> str:
        if internal_service != "continew-java" or (
            auth_enabled
            and (
                not expected_token
                or not hmac.compare_digest(supplied_token, expected_token)
            )
        ):
            raise RevisionDraftError(
                "UNAUTHORIZED_INTERNAL_CALL",
                "Internal service credential is invalid",
                status_code=401,
            )
        normalized_request_id = request_id.strip()
        if not normalized_request_id or len(normalized_request_id) > 160:
            raise RevisionDraftError(
                "INVALID_REQUEST",
                "X-Request-Id is invalid",
                status_code=400,
            )
        return normalized_request_id

    app = FastAPI(
        title="Contract Revision Draft Internal API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.revision_draft_service = service

    @app.exception_handler(RevisionDraftError)
    async def revision_error(_request: Request, exc: RevisionDraftError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "UP", "service": "contract-revision-draft"}

    @app.get(
        "/v1/internal/contract-reviews/{review_id}/revision-drafts",
        response_model=RevisionDraftResponse,
        include_in_schema=False,
    )
    async def get_revision_drafts(
        review_id: str,
        _request_id: str = Depends(authorize),
        generation_id: str = Query(min_length=1, max_length=200),
        result_hash: str = Query(pattern=r"^sha256:[0-9a-f]{64}$"),
    ) -> RevisionDraftResponse:
        coordinator = app.state.revision_draft_service
        if coordinator is None:
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                "Revision Draft service is not configured",
                status_code=503,
            )
        return await coordinator.get_or_generate(review_id, generation_id, result_hash)

    @app.post(
        "/v1/internal/contract-reviews/{review_id}/revision-drafts:generate",
        response_model=RevisionDraftResponse,
        include_in_schema=False,
    )
    async def generate_revision_drafts(
        review_id: str,
        _request_id: str = Depends(authorize),
        source: RevisionReviewSource = Body(),
    ) -> RevisionDraftResponse:
        coordinator = app.state.revision_draft_service
        if coordinator is None:
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                "Revision Draft service is not configured",
                status_code=503,
            )
        if source.review_id != review_id:
            raise RevisionDraftError(
                "REVIEW_NOT_FOUND",
                "Source review_id does not match the request path",
                status_code=404,
            )
        register = getattr(coordinator.source_provider, "register_source", None)
        if register is None:
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                "Configured source provider is read-only",
                status_code=409,
            )
        await register(source)
        return await coordinator.get_or_generate(
            source.review_id,
            source.generation_id,
            source.result_hash,
        )

    return app


def _default_service() -> RevisionDraftService | None:
    result_path = os.getenv("CONTRACT_REVISION_RESULT_FILE", "").strip()
    ir_path = os.getenv("CONTRACT_REVISION_IR_FILE", "").strip()
    generation_id = os.getenv("CONTRACT_REVISION_GENERATION_ID", "").strip()
    model_id = ModelRuntimeProvider.from_environment().active_pack.llm.id
    if not all((result_path, ir_path, generation_id)):
        return None
    return RevisionDraftService(
        source_provider=JsonRevisionSourceProvider(
            formal_payload_path=Path(result_path),
            contract_ir_path=Path(ir_path),
            generation_id=generation_id,
        ),
        generator=LlmRevisionTextGenerator(
            tenant_id=os.getenv("CONTRACT_TEST_TENANT_ID", "default"),
            model_id=model_id,
        ),
        cache=default_cache(),
    )


app = create_app(_default_service())
