from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Callable, Coroutine, Protocol

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from .domain import (
    DispatchStatus,
    InvocationLifecycleStatus,
    InvocationNotFoundError,
    InvocationOutcome,
    ModelSnapshotCapacityError,
    ModelSnapshotError,
    ModelInvocationEventType,
    PrivacyMode,
    RouteType,
    contains_sensitive_identifier_value,
    new_id,
)
from .query import (
    ModelEventFilters,
    ModelInvocationFilters,
    ModelInvocationQueryService,
    ModelQueryScope,
    ModelSummaryFilters,
)
from .schemas import (
    ErrorResponse,
    InternalModelEventList,
    InternalModelInvocationDetail,
    InternalModelInvocationList,
    InternalModelSummary,
)


_SAFE_FILTER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
_STABLE_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{2,119}$"
_STABLE_CODE = re.compile(_STABLE_CODE_PATTERN)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._:+-]*$")
_REGISTERED_ERROR_CODES = frozenset(
    {
        "OBSERVABILITY_QUERY_INVALID",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "MODEL_INVOCATION_NOT_FOUND",
        "TOO_MANY_REQUESTS",
        "OBSERVABILITY_SNAPSHOT_CAPACITY_EXCEEDED",
        "MODEL_OBSERVABILITY_SNAPSHOT_INVALID",
        "OBSERVABILITY_SOURCE_TIMEOUT",
        "MODEL_OBSERVABILITY_SOURCE_UNAVAILABLE",
        "MODEL_OBSERVABILITY_INTERNAL_ERROR",
        "SCOPE_DENIED",
    }
)


_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "参数、时间窗、快照、高水位或游标无效"},
    401: {"model": ErrorResponse, "description": "服务身份或 Scope Token 无效"},
    403: {"model": ErrorResponse, "description": "环境、权限、Path 或租户范围被拒绝"},
    429: {"model": ErrorResponse, "description": "查询或快照容量限制"},
    500: {"model": ErrorResponse, "description": "Python 内部错误"},
    503: {"model": ErrorResponse, "description": "模型观测数据源不可用"},
}


class ModelObservabilityAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        request: Request,
        scope_token: str,
        request_id: str,
    ) -> ModelQueryScope: ...


def _safe_response_request_id(value: str | None) -> str:
    if (
        isinstance(value, str)
        and value
        and len(value) <= 120
        and _SAFE_REQUEST_ID.fullmatch(value) is not None
        and not contains_sensitive_identifier_value(value)
    ):
        return value
    return new_id("request")


class _ModelObservabilityRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def protected_route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except RequestValidationError:
                return _to_error_response(
                    ValueError("request validation failed"),
                    _safe_response_request_id(request.headers.get("X-Request-ID")),
                )

        return protected_route_handler


def create_model_observability_router(
    *,
    query_service: ModelInvocationQueryService,
    authorizer: ModelObservabilityAuthorizer,
) -> APIRouter:
    """Return an unregistered router; page 8 owns root include and auth wiring."""

    router = APIRouter(
        prefix="/internal/observability/v1",
        tags=["Models"],
        route_class=_ModelObservabilityRoute,
    )

    @router.get(
        "/model-invocations",
        operation_id="internalListModelInvocations",
        response_model=InternalModelInvocationList,
        responses=_ERROR_RESPONSES,
        openapi_extra={
            "x-mtls-required": True,
            "x-fixed-sort": [
                {"field": "startedAt", "direction": "DESC"},
                {"field": "invocationId", "direction": "DESC"},
            ],
        },
    )
    async def list_model_invocations(
        request: Request,
        response: Response,
        from_time: datetime = Query(alias="from"),
        to_time: datetime = Query(alias="to"),
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None,
            alias="highWatermark",
            min_length=14,
            max_length=84,
            pattern=r"^hwm_[A-Za-z0-9_-]{10,80}$",
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        limit: int = Query(default=100, ge=1, le=200),
        task_id: str | None = Query(default=None, alias="taskId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        run_id: str | None = Query(default=None, alias="runId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        stage_id: str | None = Query(default=None, alias="stageId", min_length=1, max_length=120, pattern=_SAFE_FILTER_PATTERN),
        feature_code: str | None = Query(default=None, alias="featureCode", min_length=1, max_length=120, pattern=_SAFE_FILTER_PATTERN),
        logical_call_id: str | None = Query(default=None, alias="logicalCallId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        invocation_id: str | None = Query(default=None, alias="invocationId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        provider: str | None = Query(default=None, min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        model_pack_id: str | None = Query(default=None, alias="modelPackId", min_length=1, max_length=120, pattern=_SAFE_FILTER_PATTERN),
        model_name: str | None = Query(default=None, alias="modelName", min_length=1, max_length=160, pattern=_SAFE_FILTER_PATTERN),
        privacy_mode: PrivacyMode | None = Query(default=None, alias="privacyMode"),
        route_type: RouteType | None = Query(default=None, alias="routeType"),
        lifecycle_status: InvocationLifecycleStatus | None = Query(
            default=None, alias="lifecycleStatus"
        ),
        dispatch_status: DispatchStatus | None = Query(
            default=None, alias="dispatchStatus"
        ),
        outcome: InvocationOutcome | None = None,
        error_code: str | None = Query(default=None, alias="errorCode", min_length=3, max_length=120, pattern=_STABLE_CODE_PATTERN),
        scope_token: str = Header(
            alias="X-Observability-Scope", min_length=20, max_length=4096
        ),
        request_id: str = Header(alias="X-Request-ID"),
        traceparent: str | None = Header(default=None),
        deadline_ms: int = Header(
            alias="X-Request-Deadline-Ms", ge=100, le=5000
        ),
    ) -> InternalModelInvocationList:
        safe_request_id = _safe_response_request_id(request_id)
        del traceparent
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                scope = await authorizer.authorize(
                    request=request,
                    scope_token=scope_token,
                    request_id=safe_request_id,
                )
                result = await query_service.list_invocations(
                    scope=scope,
                    from_time=from_time,
                    to_time=to_time,
                    snapshot_to=snapshot_to,
                    filters=ModelInvocationFilters(
                        task_id=task_id,
                        run_id=run_id,
                        stage_id=stage_id,
                        feature_code=feature_code,
                        logical_call_id=logical_call_id,
                        invocation_id=invocation_id,
                        provider=provider,
                        model_pack_id=model_pack_id,
                        model_name=model_name,
                        privacy_mode=privacy_mode.value if privacy_mode else None,
                        route_type=route_type.value if route_type else None,
                        lifecycle_status=(
                            lifecycle_status.value if lifecycle_status else None
                        ),
                        dispatch_status=dispatch_status.value if dispatch_status else None,
                        outcome=outcome.value if outcome else None,
                        error_code=error_code,
                    ),
                    high_watermark=high_watermark,
                    cursor=cursor,
                    limit=limit,
                )
            _set_protected_headers(response, safe_request_id)
            return result
        except Exception as exc:
            return _to_error_response(exc, safe_request_id)

    @router.get(
        "/model-invocation-events",
        operation_id="internalListModelInvocationEvents",
        response_model=InternalModelEventList,
        responses=_ERROR_RESPONSES,
        openapi_extra={
            "x-mtls-required": True,
            "x-fixed-sort": [
                {"field": "occurredAt", "direction": "DESC"},
                {"field": "eventId", "direction": "DESC"},
            ],
        },
    )
    async def list_model_invocation_events(
        request: Request,
        response: Response,
        from_time: datetime = Query(alias="from"),
        to_time: datetime = Query(alias="to"),
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None,
            alias="highWatermark",
            min_length=14,
            max_length=84,
            pattern=r"^hwm_[A-Za-z0-9_-]{10,80}$",
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        limit: int = Query(default=100, ge=1, le=200),
        task_id: str | None = Query(default=None, alias="taskId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        run_id: str | None = Query(default=None, alias="runId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        logical_call_id: str | None = Query(default=None, alias="logicalCallId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        invocation_id: str | None = Query(default=None, alias="invocationId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        event_type: ModelInvocationEventType | None = Query(default=None, alias="eventType"),
        scope_token: str = Header(
            alias="X-Observability-Scope", min_length=20, max_length=4096
        ),
        request_id: str = Header(alias="X-Request-ID"),
        traceparent: str | None = Header(default=None),
        deadline_ms: int = Header(
            alias="X-Request-Deadline-Ms", ge=100, le=5000
        ),
    ) -> InternalModelEventList:
        safe_request_id = _safe_response_request_id(request_id)
        del traceparent
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                scope = await authorizer.authorize(
                    request=request,
                    scope_token=scope_token,
                    request_id=safe_request_id,
                )
                result = await query_service.list_events(
                    scope=scope,
                    from_time=from_time,
                    to_time=to_time,
                    snapshot_to=snapshot_to,
                    filters=ModelEventFilters(
                        task_id=task_id,
                        run_id=run_id,
                        logical_call_id=logical_call_id,
                        invocation_id=invocation_id,
                        event_type=event_type.value if event_type else None,
                    ),
                    high_watermark=high_watermark,
                    cursor=cursor,
                    limit=limit,
                )
            _set_protected_headers(response, safe_request_id)
            return result
        except Exception as exc:
            return _to_error_response(exc, safe_request_id)

    detail_responses = {
        **_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "资源不存在或 Scope 不可见"},
    }

    @router.get(
        "/model-invocations/{invocationId}",
        operation_id="internalGetModelInvocation",
        response_model=InternalModelInvocationDetail,
        responses=detail_responses,
        openapi_extra={"x-mtls-required": True, "x-detail-consistency": "LATEST"},
    )
    async def get_model_invocation(
        request: Request,
        response: Response,
        invocation_id: str = Path(alias="invocationId", min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        scope_token: str = Header(
            alias="X-Observability-Scope", min_length=20, max_length=4096
        ),
        request_id: str = Header(alias="X-Request-ID"),
        traceparent: str | None = Header(default=None),
        deadline_ms: int = Header(
            alias="X-Request-Deadline-Ms", ge=100, le=5000
        ),
    ) -> InternalModelInvocationDetail:
        safe_request_id = _safe_response_request_id(request_id)
        del traceparent
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                scope = await authorizer.authorize(
                    request=request,
                    scope_token=scope_token,
                    request_id=safe_request_id,
                )
                result = await query_service.get_invocation(
                    scope=scope,
                    invocation_id=invocation_id,
                )
            _set_protected_headers(response, safe_request_id)
            return result
        except Exception as exc:
            return _to_error_response(exc, safe_request_id)

    @router.get(
        "/model-summary",
        operation_id="internalGetModelSummary",
        response_model=InternalModelSummary,
        responses=_ERROR_RESPONSES,
        openapi_extra={"x-mtls-required": True},
    )
    async def get_model_summary(
        request: Request,
        response: Response,
        from_time: datetime = Query(alias="from"),
        to_time: datetime = Query(alias="to"),
        feature_code: str | None = Query(default=None, alias="featureCode", min_length=1, max_length=120, pattern=_SAFE_FILTER_PATTERN),
        provider: str | None = Query(default=None, min_length=1, max_length=80, pattern=_SAFE_FILTER_PATTERN),
        model_pack_id: str | None = Query(default=None, alias="modelPackId", min_length=1, max_length=120, pattern=_SAFE_FILTER_PATTERN),
        model_name: str | None = Query(default=None, alias="modelName", min_length=1, max_length=160, pattern=_SAFE_FILTER_PATTERN),
        privacy_mode: PrivacyMode | None = Query(default=None, alias="privacyMode"),
        route_type: RouteType | None = Query(default=None, alias="routeType"),
        scope_token: str = Header(
            alias="X-Observability-Scope", min_length=20, max_length=4096
        ),
        request_id: str = Header(alias="X-Request-ID"),
        traceparent: str | None = Header(default=None),
        deadline_ms: int = Header(
            alias="X-Request-Deadline-Ms", ge=100, le=5000
        ),
    ) -> InternalModelSummary:
        safe_request_id = _safe_response_request_id(request_id)
        del traceparent
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                scope = await authorizer.authorize(
                    request=request,
                    scope_token=scope_token,
                    request_id=safe_request_id,
                )
                result = await query_service.summary(
                    scope=scope,
                    from_time=from_time,
                    to_time=to_time,
                    filters=ModelSummaryFilters(
                        feature_code=feature_code,
                        provider=provider,
                        model_pack_id=model_pack_id,
                        model_name=model_name,
                        privacy_mode=privacy_mode.value if privacy_mode else None,
                        route_type=route_type.value if route_type else None,
                    ),
                )
            _set_protected_headers(response, safe_request_id)
            return result
        except Exception as exc:
            return _to_error_response(exc, safe_request_id)

    return router


def _safe_error_code(value: object, status: int) -> str:
    if (
        isinstance(value, str)
        and _STABLE_CODE.fullmatch(value) is not None
        and value in _REGISTERED_ERROR_CODES
        and not contains_sensitive_identifier_value(value)
    ):
        return value
    return _http_error_code(status)


def _to_error_response(exc: Exception, request_id: str) -> JSONResponse:
    if isinstance(exc, HTTPException):
        status = exc.status_code
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code") or _http_error_code(status)
        retryable = bool(detail.get("retryable", status in {429, 503}))
    elif isinstance(exc, InvocationNotFoundError):
        status, code, retryable = 404, exc.code, False
    elif isinstance(exc, ModelSnapshotCapacityError):
        status, code, retryable = 429, exc.code, True
    elif isinstance(exc, ModelSnapshotError):
        status, code, retryable = 400, exc.code, False
    elif isinstance(exc, ValueError):
        status, code, retryable = 400, "OBSERVABILITY_QUERY_INVALID", False
    elif isinstance(exc, TimeoutError):
        status, code, retryable = 503, "OBSERVABILITY_SOURCE_TIMEOUT", True
    elif isinstance(exc, SQLAlchemyError):
        status, code, retryable = 503, "MODEL_OBSERVABILITY_SOURCE_UNAVAILABLE", True
    else:
        status, code, retryable = 500, "MODEL_OBSERVABILITY_INTERNAL_ERROR", False
    code = _safe_error_code(code, status)
    body = ErrorResponse(code=code, request_id=request_id, retryable=retryable)
    return JSONResponse(
        status_code=status,
        content=body.model_dump(by_alias=True),
        headers=_protected_header_values(request_id),
    )


def _set_protected_headers(response: Response, request_id: str) -> None:
    for name, value in _protected_header_values(request_id).items():
        response.headers[name] = value


def _protected_header_values(request_id: str) -> dict[str, str]:
    return {
        "X-Request-ID": request_id,
        "Cache-Control": "no-store, private",
        "Referrer-Policy": "no-referrer",
        "Vary": "Authorization, X-Observability-Scope",
    }


def _http_error_code(status: int) -> str:
    return {
        400: "OBSERVABILITY_QUERY_INVALID",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "MODEL_INVOCATION_NOT_FOUND",
        429: "TOO_MANY_REQUESTS",
        503: "MODEL_OBSERVABILITY_SOURCE_UNAVAILABLE",
    }.get(status, "MODEL_OBSERVABILITY_INTERNAL_ERROR")
