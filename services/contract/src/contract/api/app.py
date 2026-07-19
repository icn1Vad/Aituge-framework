from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile, status
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
from contract.config import Settings, get_settings
from contract.errors import ContractError


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


def create_app(
    settings: Settings | None = None,
    service: ContractReviewService | None = None,
) -> FastAPI:
    app = FastAPI(title="Contract Agent", version="1.0.0")
    app.state.settings = settings or get_settings()
    app.state.contract_service = service

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
        context: Annotated[InternalRequestContext, Depends(_internal_context)],
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


def _internal_context(
    request: Request,
    internal_service: Annotated[str, Header(alias="X-Internal-Service")],
    internal_token: Annotated[str, Header(alias="X-Internal-Token")],
    user_id: Annotated[str, Header(alias="X-User-Id")],
    tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    request_id: Annotated[str, Header(alias="X-Request-Id")],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
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
        idempotency_key=_optional_header(idempotency_key),
    )


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
