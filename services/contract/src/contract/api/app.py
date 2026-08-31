from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, File, Form, Header, Query, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from aituge_model.config import ModelRuntimeProvider, get_model_pack_for_ai_mode

from contract.api.models import (
    CancelReviewData,
    CreateReviewData,
    CreateReviewRequest,
    ErrorData,
    ErrorResponse,
    HealthData,
    PartyResolutionCreateData,
    PartyResolutionCreateRequest,
    PartyResolutionStatusData,
    PublicReviewResultData,
    ReviewStatus,
    ReviewStatusData,
    SuccessResponse,
)
from contract.application.dispatcher import ContractDispatcher
from contract.application.document_processing import ContractDocumentProcessor
from contract.application.mock_service import InMemoryContractReviewService
from contract.application.ports import ContractReviewService, InternalRequestContext, UploadedContract
from contract.application.runtime_service import build_runtime_contract_review_service
from contract.callback.models import (
    FrameworkCallback,
    FrameworkCallbackData,
    GatewayStageResult,
    StageExecuteRequest,
)
from contract.callback.service import FrameworkCallbackService
from contract.config import Settings, get_settings
from contract.errors import ContractError
from contract.grounded.models import (
    GroundedAnswerData,
    GroundedChatRequest,
    GroundedReportRequest,
)
from contract.grounded.service import FrameworkGroundedAnswerService
from contract.internal.models import (
    ContractBlocksToolData,
    ContractBlocksToolRequest,
    ContractClauseContextToolData,
    ContractClauseContextToolRequest,
    ContractDocumentToolData,
    ContractDocumentToolRequest,
    ContractIrToolData,
    ContractIrToolRequest,
    ContractReviewResultToolData,
    ContractReviewResultToolRequest,
    ContractRiskPlanRequest,
    ContractLegalEvidenceRequest,
    ContractLegalEvidenceToolData,
    ContractWindowPlanToolData,
    ContractWindowPlanToolRequest,
)
from contract.risk.models import RiskReviewPlan
from contract.legal_evidence.provider import build_legal_evidence_provider
from contract.internal.service import ContractInternalService
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.repository import ContractRepository
from services.contract.capabilities.revision_drafts import (
    LlmRevisionTextGenerator,
    PostgresRevisionSourceProvider,
    RevisionDraftError,
    RevisionDraftResponse,
    RevisionDraftService,
    default_cache,
)
from contract.revision_llm_gateway import FrameworkRevisionLlmRuntime
from contract.observability import configure_tracing_from_env


ALLOWED_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
OLE_COMPOUND_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    service: ContractReviewService | None = None,
    internal_service: ContractInternalService | None = None,
    callback_service: FrameworkCallbackService | None = None,
    revision_draft_service: RevisionDraftService | None = None,
    grounded_answer_service: FrameworkGroundedAnswerService | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        dispatcher_task: asyncio.Task | None = None
        dispatcher_stop: asyncio.Event | None = None
        if not application.state.settings.mock_mode:
            runtime_service = application.state.contract_service
            if runtime_service is None:
                runtime_service = build_runtime_contract_review_service(application.state.settings)
                application.state.contract_service = runtime_service
            reconcile = getattr(runtime_service, "reconcile_nonterminal_reviews", None)
            if callable(reconcile):
                count = await asyncio.to_thread(reconcile)
                logger.info("Reconciled %s nonterminal contract reviews during startup", count)
            if callable(getattr(runtime_service, "dispatch_pending_attempts", None)):
                dispatcher = ContractDispatcher(runtime_service, application.state.settings)
                dispatcher_stop = asyncio.Event()
                application.state.contract_dispatcher = dispatcher
                dispatcher_task = asyncio.create_task(dispatcher.run_forever(dispatcher_stop))
        try:
            yield
        finally:
            if dispatcher_stop is not None:
                dispatcher_stop.set()
            if dispatcher_task is not None:
                await dispatcher_task

    app = FastAPI(title="Contract Agent", version="1.0.0", lifespan=lifespan)
    configure_tracing_from_env(app, default_service_name="contract-review-contract")
    app.state.settings = app_settings
    app.state.contract_service = service
    app.state.contract_internal_service = internal_service
    app.state.framework_callback_service = callback_service
    app.state.revision_draft_service = revision_draft_service
    app.state.revision_draft_cache = None
    app.state.grounded_answer_service = grounded_answer_service

    @app.exception_handler(ContractError)
    async def handle_contract_error(request: Request, exc: ContractError) -> JSONResponse:
        payload = ErrorResponse(
            error=ErrorData(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                user_action_required=exc.user_action_required,
                details=exc.details,
            ),
            request_id=_request_id(request),
        )
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = {
            "violations": [
                {
                    "location": [str(item) for item in error.get("loc", ())],
                    "message": error.get("msg", "Invalid value"),
                    "type": error.get("type", "validation_error"),
                }
                for error in exc.errors()
            ]
        }
        payload = ErrorResponse(
            error=ErrorData(
                code="REQUEST_SCHEMA_INVALID",
                message="请求参数不符合合同审查协议",
                retryable=False,
                user_action_required=True,
                details=details,
            ),
            request_id=_request_id(request),
        )
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        payload = ErrorResponse(
            error=ErrorData(
                code="INTERNAL_ERROR",
                message="合同审查服务发生内部错误",
                retryable=False,
                user_action_required=False,
                details=None,
            ),
            request_id=_request_id(request),
        )
        return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))

    @app.get("/health", response_model=SuccessResponse[HealthData])
    async def health(request: Request) -> SuccessResponse[HealthData]:
        data = await asyncio.to_thread(_service(request).health)
        return SuccessResponse(data=HealthData.model_validate(data), request_id=_request_id(request))

    @app.post(
        "/v1/contract-reviews",
        status_code=status.HTTP_201_CREATED,
        response_model=SuccessResponse[CreateReviewData],
        responses=ERROR_RESPONSES,
    )
    async def create_review(
        http_request: Request,
        file: Annotated[UploadFile, File(...)],
        request_payload: Annotated[str, Form(alias="request")],
        context: Annotated[InternalRequestContext, Depends(_create_internal_context)],
    ) -> SuccessResponse[CreateReviewData]:
        settings = http_request.app.state.settings
        try:
            payload = CreateReviewRequest.model_validate_json(request_payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "REQUEST_SCHEMA_INVALID",
                "request Part不是有效的schema_version=1.0 JSON",
                status_code=422,
                user_action_required=True,
                details={"reason": _safe_validation_reason(exc)},
            ) from exc
        ai_mode = str(context.ai_mode or "").strip()
        if ai_mode:
            try:
                payload = payload.model_copy(
                    update={"model_pack_id": get_model_pack_for_ai_mode(ai_mode).id}
                )
            except ValueError as exc:
                raise ContractError(
                    "INVALID_AI_MODE",
                    "X-AI-Mode必须为public或private，且映射到已注册的模型包",
                    status_code=400,
                    user_action_required=True,
                ) from exc
        content = await file.read(settings.max_file_size + 1)
        upload = _validate_upload(
            filename=file.filename or "",
            content_type=file.content_type or "",
            content=content,
            max_file_size=settings.max_file_size,
        )
        data = await asyncio.to_thread(
            _service(http_request).create_review,
            upload=upload,
            request=payload,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.post(
        "/v1/contract-party-resolutions",
        status_code=status.HTTP_201_CREATED,
        response_model=SuccessResponse[PartyResolutionCreateData],
        responses=ERROR_RESPONSES,
    )
    async def create_party_resolution(
        http_request: Request,
        file: Annotated[UploadFile, File(...)],
        request_payload: Annotated[str, Form(alias="request")],
        context: Annotated[InternalRequestContext, Depends(_create_internal_context)],
    ) -> SuccessResponse[PartyResolutionCreateData]:
        settings = http_request.app.state.settings
        try:
            payload = PartyResolutionCreateRequest.model_validate_json(request_payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "REQUEST_SCHEMA_INVALID",
                "request Part is not valid schema_version=1.0 JSON",
                status_code=422,
                user_action_required=True,
                details={"reason": _safe_validation_reason(exc)},
            ) from exc
        ai_mode = str(context.ai_mode or "").strip()
        if ai_mode:
            try:
                payload = payload.model_copy(
                    update={"model_pack_id": get_model_pack_for_ai_mode(ai_mode).id}
                )
            except ValueError as exc:
                raise ContractError(
                    "INVALID_AI_MODE",
                    "X-AI-Mode must map to a registered model pack",
                    status_code=400,
                    user_action_required=True,
                ) from exc
        content = await file.read(settings.max_file_size + 1)
        upload = _validate_upload(
            filename=file.filename or "",
            content_type=file.content_type or "",
            content=content,
            max_file_size=settings.max_file_size,
        )
        data = await asyncio.to_thread(
            _service(http_request).create_party_resolution,
            upload=upload,
            request=payload,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.get(
        "/v1/contract-party-resolutions/{resolution_id}",
        response_model=SuccessResponse[PartyResolutionStatusData],
        responses=ERROR_RESPONSES,
    )
    async def get_party_resolution(
        resolution_id: str,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
    ) -> SuccessResponse[PartyResolutionStatusData]:
        data = await asyncio.to_thread(
            _service(http_request).get_party_resolution,
            resolution_id,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.post(
        "/v1/internal/contract-reviews/{review_id}/framework-result",
        response_model=SuccessResponse[FrameworkCallbackData],
        responses=ERROR_RESPONSES,
    )
    async def accept_framework_result(
        review_id: str,
        http_request: Request,
        callback: Annotated[FrameworkCallback, Body(discriminator="callback_type")],
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[FrameworkCallbackData]:
        data = await asyncio.to_thread(
            _callback_service(http_request).accept,
            review_id,
            callback,
        )
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-reviews/{review_id}/stages/execute",
        response_model=SuccessResponse[GatewayStageResult],
        responses=ERROR_RESPONSES,
    )
    async def execute_framework_stage(
        review_id: str,
        http_request: Request,
        payload: StageExecuteRequest,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[GatewayStageResult]:
        if payload.review_id != review_id:
            raise ContractError(
                "FRAMEWORK_CALLBACK_MISMATCH",
                "Stage review_id does not match the request path",
                status_code=409,
            )
        data = await asyncio.to_thread(_internal_service(http_request).execute_stage, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-tools/document",
        response_model=SuccessResponse[ContractDocumentToolData],
        responses=ERROR_RESPONSES,
    )
    async def contract_get_document(
        payload: ContractDocumentToolRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractDocumentToolData]:
        data = await asyncio.to_thread(_internal_service(http_request).get_document, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-tools/blocks",
        response_model=SuccessResponse[ContractBlocksToolData],
        responses=ERROR_RESPONSES,
    )
    async def contract_get_blocks(
        payload: ContractBlocksToolRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractBlocksToolData]:
        data = await asyncio.to_thread(_internal_service(http_request).get_blocks, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-tools/clause-context",
        response_model=SuccessResponse[ContractClauseContextToolData],
        responses=ERROR_RESPONSES,
    )
    async def contract_get_clause_context(
        payload: ContractClauseContextToolRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractClauseContextToolData]:
        data = await asyncio.to_thread(_internal_service(http_request).get_clause_context, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-tools/ir",
        response_model=SuccessResponse[ContractIrToolData],
        responses=ERROR_RESPONSES,
    )
    async def contract_get_ir(
        payload: ContractIrToolRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractIrToolData]:
        data = await asyncio.to_thread(_internal_service(http_request).get_ir, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-tools/review-result",
        response_model=SuccessResponse[ContractReviewResultToolData],
        responses=ERROR_RESPONSES,
    )
    async def contract_get_review_result(
        payload: ContractReviewResultToolRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractReviewResultToolData]:
        data = await asyncio.to_thread(
            _internal_service(http_request).get_review_result,
            payload,
        )
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-tools/windows",
        response_model=SuccessResponse[ContractWindowPlanToolData],
        responses=ERROR_RESPONSES,
        include_in_schema=False,
    )
    async def contract_get_windows(
        payload: ContractWindowPlanToolRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractWindowPlanToolData]:
        data = await asyncio.to_thread(_internal_service(http_request).get_window_plan, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-reviews/{review_id}/risk-plan",
        response_model=SuccessResponse[RiskReviewPlan],
        responses=ERROR_RESPONSES,
        include_in_schema=False,
    )
    async def contract_get_risk_plan(
        review_id: str,
        payload: ContractRiskPlanRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[RiskReviewPlan]:
        if payload.review_id != review_id:
            raise ContractError(
                "FRAMEWORK_CALLBACK_MISMATCH",
                "Risk plan review_id does not match the request path",
                status_code=409,
            )
        data = await asyncio.to_thread(_internal_service(http_request).get_risk_plan, payload)
        return SuccessResponse(data=data, request_id=request_id)

    @app.post(
        "/v1/internal/contract-reviews/{review_id}/legal-evidence",
        response_model=SuccessResponse[ContractLegalEvidenceToolData],
        responses=ERROR_RESPONSES,
        include_in_schema=False,
    )
    async def contract_get_legal_evidence(
        review_id: str,
        payload: ContractLegalEvidenceRequest,
        http_request: Request,
        request_id: Annotated[str, Depends(_framework_request_id)],
    ) -> SuccessResponse[ContractLegalEvidenceToolData]:
        if payload.plan_input.review_id != review_id:
            raise ContractError(
                "FRAMEWORK_CALLBACK_MISMATCH",
                "Legal evidence review_id does not match the request path",
                status_code=409,
            )
        data = await asyncio.to_thread(
            _internal_service(http_request).get_legal_evidence, payload
        )
        return SuccessResponse(data=data, request_id=request_id)

    @app.get(
        "/v1/internal/contract-reviews/{review_id}/revision-drafts",
        response_model=RevisionDraftResponse,
        responses=ERROR_RESPONSES,
        include_in_schema=False,
    )
    async def get_revision_drafts(
        review_id: str,
        http_request: Request,
        result_hash: Annotated[str, Query(pattern=r"^sha256:[0-9a-f]{64}$")],
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
        generation_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> RevisionDraftResponse:
        return await _generate_revision_drafts(
            http_request,
            context,
            review_id=review_id,
            generation_id=generation_id,
            result_hash=result_hash,
        )

    @app.post(
        "/v1/internal/contract-reviews/{review_id}/revision-drafts:generate",
        response_model=RevisionDraftResponse,
        responses=ERROR_RESPONSES,
        include_in_schema=False,
    )
    async def generate_revision_drafts(
        review_id: str,
        http_request: Request,
        result_hash: Annotated[str, Query(pattern=r"^sha256:[0-9a-f]{64}$")],
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
        generation_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> RevisionDraftResponse:
        return await _generate_revision_drafts(
            http_request,
            context,
            review_id=review_id,
            generation_id=generation_id,
            result_hash=result_hash,
        )

    @app.get(
        "/v1/contract-reviews/{review_id}",
        response_model=SuccessResponse[ReviewStatusData],
        responses=ERROR_RESPONSES,
    )
    async def get_review_status(
        review_id: str,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
    ) -> SuccessResponse[ReviewStatusData]:
        data = await asyncio.to_thread(
            _service(http_request).get_status,
            review_id,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.get(
        "/v1/contract-reviews/{review_id}/result",
        response_model=SuccessResponse[PublicReviewResultData],
        responses=ERROR_RESPONSES,
    )
    async def get_review_result(
        review_id: str,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
    ) -> SuccessResponse[PublicReviewResultData]:
        data = await asyncio.to_thread(
            _service(http_request).get_result,
            review_id,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.post(
        "/v1/contract-reviews/{review_id}/report",
        response_model=SuccessResponse[GroundedAnswerData],
        responses=ERROR_RESPONSES,
    )
    async def generate_grounded_report(
        review_id: str,
        payload: GroundedReportRequest,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_create_internal_context)],
    ) -> SuccessResponse[GroundedAnswerData]:
        await _require_grounded_review(
            http_request,
            context,
            review_id=review_id,
            document_id=payload.document_id,
        )
        service = _grounded_answer_service(http_request)
        data = await service.generate_report(
            review_id=review_id,
            request=payload,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.post(
        "/v1/contract-reviews/{review_id}/chat",
        response_model=SuccessResponse[GroundedAnswerData],
        responses=ERROR_RESPONSES,
    )
    async def answer_grounded_chat(
        review_id: str,
        payload: GroundedChatRequest,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_create_internal_context)],
    ) -> SuccessResponse[GroundedAnswerData]:
        await _require_grounded_review(
            http_request,
            context,
            review_id=review_id,
            document_id=payload.document_id,
        )
        service = _grounded_answer_service(http_request)
        data = await service.answer_chat(
            review_id=review_id,
            request=payload,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    @app.post(
        "/v1/contract-reviews/{review_id}/chat/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "合同问答SSE事件流",
                "content": {
                    "text/event-stream": {
                        "schema": {"type": "string"},
                    }
                },
            },
            **ERROR_RESPONSES,
        },
    )
    async def stream_grounded_chat(
        review_id: str,
        payload: GroundedChatRequest,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_create_internal_context)],
    ) -> StreamingResponse:
        await _require_grounded_review(
            http_request,
            context,
            review_id=review_id,
            document_id=payload.document_id,
        )
        service = _grounded_answer_service(http_request)

        async def event_stream():
            try:
                async for event_type, event_data in service.stream_chat(
                    review_id=review_id,
                    request=payload,
                    context=context,
                ):
                    yield _sse_event(event_type, event_data)
            except ContractError as exc:
                yield _sse_event(
                    "error",
                    {
                        "request_id": context.request_id,
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            "retryable": exc.retryable,
                            "user_action_required": exc.user_action_required,
                            "details": exc.details,
                        },
                    },
                )
            except Exception:
                logging.exception(
                    "Unexpected grounded chat stream failure; request_id=%s",
                    context.request_id,
                )
                yield _sse_event(
                    "error",
                    {
                        "request_id": context.request_id,
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "合同问答流式生成失败",
                            "retryable": False,
                            "user_action_required": False,
                            "details": None,
                        },
                    },
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/v1/contract-reviews/{review_id}/cancel",
        response_model=SuccessResponse[CancelReviewData],
        responses=ERROR_RESPONSES,
    )
    async def cancel_review(
        review_id: str,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
    ) -> SuccessResponse[CancelReviewData]:
        data = await asyncio.to_thread(
            _service(http_request).cancel_review,
            review_id,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

    def contract_openapi() -> dict:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path in ("/v1/contract-reviews", "/v1/contract-party-resolutions"):
            multipart = schema["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]
            multipart["encoding"] = {"request": {"contentType": "application/json"}}
            body_schema_name = multipart["schema"]["$ref"].rsplit("/", 1)[-1]
            body_schema = schema["components"]["schemas"][body_schema_name]
            properties = body_schema["properties"]
            if "request_payload" in properties:
                properties["request"] = properties.pop("request_payload")
            body_schema["required"] = [
                "request" if field == "request_payload" else field
                for field in body_schema.get("required", [])
            ]
        app.openapi_schema = schema
        return schema

    app.openapi = contract_openapi  # type: ignore[method-assign]
    return app


async def _require_grounded_review(
    request: Request,
    context: InternalRequestContext,
    *,
    review_id: str,
    document_id: str,
) -> None:
    review = await asyncio.to_thread(
        _service(request).get_status,
        review_id,
        context=context,
    )
    if review.status != ReviewStatus.SUCCEEDED:
        raise ContractError(
            "REVIEW_NOT_READY",
            "合同审查稳定结果尚未完成",
            status_code=409,
            user_action_required=True,
            details={"current_status": review.status.value},
        )
    if review.document_id != document_id:
        raise ContractError(
            "REVIEW_DOCUMENT_MISMATCH",
            "document_id与合同审查任务不一致",
            status_code=409,
            user_action_required=True,
        )


def _grounded_answer_service(request: Request) -> FrameworkGroundedAnswerService:
    configured = request.app.state.grounded_answer_service
    if configured is not None:
        return configured
    configured = FrameworkGroundedAnswerService(request.app.state.settings)
    request.app.state.grounded_answer_service = configured
    return configured


def _sse_event(event_type: str, data: dict[str, object]) -> str:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def _generate_revision_drafts(
    request: Request,
    context: InternalRequestContext,
    *,
    review_id: str,
    generation_id: str | None,
    result_hash: str,
) -> RevisionDraftResponse:
    try:
        service = _revision_draft_service(request, context)
        resolver = getattr(service.source_provider, "resolve_generation_id", None)
        if callable(resolver):
            resolved_generation_id = await resolver(review_id, result_hash)
            if generation_id is not None and generation_id != resolved_generation_id:
                raise RevisionDraftError(
                    "GENERATION_NOT_FOUND",
                    "generation_id does not match the completed review",
                    status_code=404,
                )
            generation_id = resolved_generation_id
        elif generation_id is None:
            raise RevisionDraftError(
                "GENERATION_NOT_FOUND",
                "Completed review Generation was not found",
                status_code=404,
            )
        return await service.get_or_generate(
            review_id,
            generation_id,
            result_hash,
        )
    except RevisionDraftError as exc:
        raise ContractError(
            exc.code,
            str(exc),
            status_code=exc.status_code,
            retryable=exc.status_code >= 500,
        ) from exc


def _revision_draft_service(
    request: Request,
    context: InternalRequestContext,
) -> RevisionDraftService:
    configured = request.app.state.revision_draft_service
    if configured is not None:
        return configured
    _ensure_internal_components(request)
    cache = request.app.state.revision_draft_cache
    if cache is None:
        cache = default_cache()
        request.app.state.revision_draft_cache = cache
    model_id = ModelRuntimeProvider.from_environment().active_pack.llm.id
    callback_repository = request.app.state.contract_internal_service.callback_repository
    return RevisionDraftService(
        source_provider=PostgresRevisionSourceProvider(
            repository=callback_repository,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        ),
        generator=LlmRevisionTextGenerator(
            tenant_id=context.tenant_id,
            model_id=model_id,
            runtime=FrameworkRevisionLlmRuntime(
                base_url=request.app.state.settings.framework_base_url,
                internal_token=request.app.state.settings.internal_token,
                tenant_id=context.tenant_id,
                connect_timeout_seconds=(
                    request.app.state.settings.framework_connect_timeout_seconds
                ),
                read_timeout_seconds=(
                    request.app.state.settings.revision_draft_read_timeout_seconds
                ),
            ),
        ),
        cache=cache,
    )


def _service(request: Request) -> ContractReviewService:
    service = request.app.state.contract_service
    if service is not None:
        return service
    settings: Settings = request.app.state.settings
    service = (
        InMemoryContractReviewService()
        if settings.mock_mode
        else build_runtime_contract_review_service(settings)
    )
    request.app.state.contract_service = service
    return service


def _internal_service(request: Request) -> ContractInternalService:
    _ensure_internal_components(request)
    return request.app.state.contract_internal_service


def _callback_service(request: Request) -> FrameworkCallbackService:
    _ensure_internal_components(request)
    return request.app.state.framework_callback_service


def _ensure_internal_components(request: Request) -> None:
    if (
        request.app.state.contract_internal_service is not None
        and request.app.state.framework_callback_service is not None
    ):
        return
    settings: Settings = request.app.state.settings
    repository = ContractRepository(settings)
    callback_repository = FrameworkCallbackRepository(settings)
    document_processor = ContractDocumentProcessor(settings, repository)
    internal_service = ContractInternalService(
        repository,
        callback_repository,
        document_processor=document_processor,
        legal_evidence_provider=build_legal_evidence_provider(settings),
    )
    request.app.state.contract_internal_service = internal_service
    request.app.state.framework_callback_service = FrameworkCallbackService(
        callback_repository,
        internal_service,
    )


def _create_internal_context(
    request: Request,
    internal_service: Annotated[str, Header(alias="X-Internal-Service")],
    internal_token: Annotated[str, Header(alias="X-Internal-Token")],
    user_id: Annotated[str, Header(alias="X-User-Id")],
    tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    request_id: Annotated[str, Header(alias="X-Request-Id")],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    ai_mode: Annotated[str | None, Header(alias="X-AI-Mode")] = None,
) -> InternalRequestContext:
    return _java_context(
        request,
        internal_service=internal_service,
        internal_token=internal_token,
        user_id=user_id,
        tenant_id=tenant_id,
        request_id=request_id,
        idempotency_key=_optional_header(idempotency_key),
        ai_mode=_optional_header(ai_mode),
    )


def _internal_context(
    request: Request,
    internal_service: Annotated[str, Header(alias="X-Internal-Service")],
    internal_token: Annotated[str, Header(alias="X-Internal-Token")],
    user_id: Annotated[str, Header(alias="X-User-Id")],
    tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    request_id: Annotated[str, Header(alias="X-Request-Id")],
    ai_mode: Annotated[str | None, Header(alias="X-AI-Mode")] = None,
) -> InternalRequestContext:
    return _java_context(
        request,
        internal_service=internal_service,
        internal_token=internal_token,
        user_id=user_id,
        tenant_id=tenant_id,
        request_id=request_id,
        idempotency_key=None,
        ai_mode=_optional_header(ai_mode),
    )


def _java_context(
    request: Request,
    *,
    internal_service: str,
    internal_token: str,
    user_id: str,
    tenant_id: str,
    request_id: str,
    idempotency_key: str | None,
    ai_mode: str | None,
) -> InternalRequestContext:
    settings: Settings = request.app.state.settings
    if internal_service != "continew-java":
        raise ContractError(
            "UNAUTHORIZED_INTERNAL_CALL",
            "不允许的内部调用方",
            status_code=401,
        )
    if settings.internal_auth_enabled:
        if not settings.internal_token or not hmac.compare_digest(internal_token, settings.internal_token):
            raise ContractError(
                "UNAUTHORIZED_INTERNAL_CALL",
                "内部服务凭证无效",
                status_code=401,
            )
    return InternalRequestContext(
        tenant_id=_required_header("X-Tenant-Id", tenant_id),
        user_id=_required_header("X-User-Id", user_id),
        request_id=_required_header("X-Request-Id", request_id),
        idempotency_key=idempotency_key,
        ai_mode=ai_mode,
    )


def _framework_request_id(
    request: Request,
    internal_service: Annotated[str, Header(alias="X-Internal-Service")],
    internal_token: Annotated[str, Header(alias="X-Internal-Token")],
    request_id: Annotated[str, Header(alias="X-Request-Id")],
) -> str:
    settings: Settings = request.app.state.settings
    if internal_service != "aituge-framework":
        raise ContractError(
            "UNAUTHORIZED_INTERNAL_CALL",
            "Internal caller is not allowed",
            status_code=401,
        )
    expected = settings.framework_result_sink_internal_token
    if settings.internal_auth_enabled and (
        not expected or not hmac.compare_digest(internal_token, expected)
    ):
        raise ContractError(
            "UNAUTHORIZED_INTERNAL_CALL",
            "Framework callback credential is invalid",
            status_code=401,
        )
    return _required_header("X-Request-Id", request_id)


def _required_header(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 160:
        raise ContractError(
            "INVALID_REQUEST",
            f"{name}不能为空且长度不能超过160",
            status_code=400,
            user_action_required=True,
        )
    return normalized


def _optional_header(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ContractError(
            "INVALID_REQUEST",
            "Idempotency-Key不能为空且长度不能超过200",
            status_code=400,
            user_action_required=True,
        )
    return normalized


def _validate_upload(
    *,
    filename: str,
    content_type: str,
    content: bytes,
    max_file_size: int,
) -> UploadedContract:
    if len(content) > max_file_size:
        raise ContractError(
            "FILE_TOO_LARGE",
            "合同文件不能超过25 MiB",
            status_code=413,
            user_action_required=True,
            details={"max_bytes": max_file_size},
        )
    extension = Path(filename).suffix.lower()
    expected_content_type = ALLOWED_FILE_TYPES.get(extension)
    if expected_content_type is None or content_type != expected_content_type:
        raise ContractError(
            "FILE_TYPE_UNSUPPORTED",
            "第一阶段仅支持具有文本层的PDF和DOCX合同",
            status_code=415,
            user_action_required=True,
            details={"supported_content_types": list(ALLOWED_FILE_TYPES.values())},
        )
    if not content:
        raise ContractError(
            "FILE_CORRUPTED",
            "合同文件为空或已损坏",
            status_code=422,
            user_action_required=True,
        )
    if extension == ".pdf" and not content.startswith(b"%PDF-"):
        raise ContractError(
            "FILE_CORRUPTED",
            "PDF文件签名无效",
            status_code=422,
            user_action_required=True,
        )
    if extension == ".docx":
        if content.startswith(OLE_COMPOUND_SIGNATURE):
            raise ContractError(
                "FILE_ENCRYPTED",
                "第一阶段不支持加密DOCX合同",
                status_code=422,
                user_action_required=True,
            )
        if not content.startswith(b"PK"):
            raise ContractError(
                "FILE_CORRUPTED",
                "DOCX文件签名无效",
                status_code=422,
                user_action_required=True,
            )
    return UploadedContract(filename=filename, content_type=content_type, content=content)


def _request_id(request: Request) -> str:
    value = request.headers.get("X-Request-Id", "").strip()
    if 0 < len(value) <= 160:
        return value
    return f"req-{uuid.uuid4().hex}"


def _safe_validation_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )[:2000]
    return "invalid_json"


app = create_app()
