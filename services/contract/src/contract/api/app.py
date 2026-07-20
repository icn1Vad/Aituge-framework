from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, File, Form, Header, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from contract.api.models import (
    CancelReviewData,
    CreateReviewData,
    CreateReviewRequest,
    ErrorData,
    ErrorResponse,
    HealthData,
    ReviewResultData,
    ReviewStatusData,
    SuccessResponse,
)
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
from contract.internal.models import (
    ContractBlocksToolData,
    ContractBlocksToolRequest,
    ContractClauseContextToolData,
    ContractClauseContextToolRequest,
    ContractDocumentToolData,
    ContractDocumentToolRequest,
    ContractIrToolData,
    ContractIrToolRequest,
)
from contract.internal.service import ContractInternalService
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.repository import ContractRepository


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
) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if not application.state.settings.mock_mode:
            runtime_service = application.state.contract_service
            if runtime_service is None:
                runtime_service = build_runtime_contract_review_service(application.state.settings)
                application.state.contract_service = runtime_service
            reconcile = getattr(runtime_service, "reconcile_nonterminal_reviews", None)
            if callable(reconcile):
                count = await asyncio.to_thread(reconcile)
                logger.info("Reconciled %s nonterminal contract reviews during startup", count)
        yield

    app = FastAPI(title="Contract Agent", version="1.0.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.contract_service = service
    app.state.contract_internal_service = internal_service
    app.state.framework_callback_service = callback_service

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
        response_model=SuccessResponse[ReviewResultData],
        responses=ERROR_RESPONSES,
    )
    async def get_review_result(
        review_id: str,
        http_request: Request,
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
    ) -> SuccessResponse[ReviewResultData]:
        data = await asyncio.to_thread(
            _service(http_request).get_result,
            review_id,
            context=context,
        )
        return SuccessResponse(data=data, request_id=context.request_id)

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
        multipart = schema["paths"]["/v1/contract-reviews"]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]
        multipart["encoding"] = {"request": {"contentType": "application/json"}}
        app.openapi_schema = schema
        return schema

    app.openapi = contract_openapi  # type: ignore[method-assign]
    return app


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
    internal_service = ContractInternalService(repository, callback_repository)
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
) -> InternalRequestContext:
    return _java_context(
        request,
        internal_service=internal_service,
        internal_token=internal_token,
        user_id=user_id,
        tenant_id=tenant_id,
        request_id=request_id,
        idempotency_key=_optional_header(idempotency_key),
    )


def _internal_context(
    request: Request,
    internal_service: Annotated[str, Header(alias="X-Internal-Service")],
    internal_token: Annotated[str, Header(alias="X-Internal-Token")],
    user_id: Annotated[str, Header(alias="X-User-Id")],
    tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    request_id: Annotated[str, Header(alias="X-Request-Id")],
) -> InternalRequestContext:
    return _java_context(
        request,
        internal_service=internal_service,
        internal_token=internal_token,
        user_id=user_id,
        tenant_id=tenant_id,
        request_id=request_id,
        idempotency_key=None,
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
