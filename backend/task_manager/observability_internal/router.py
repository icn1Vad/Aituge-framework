from __future__ import annotations

# ruff: noqa: B008 - FastAPI parameter objects are intentional defaults.
import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError

from .auth import HmacInternalAuthenticator, InternalAuthContext, normalize_request_id
from .errors import (
    InternalObservabilityError,
    ResourceAccessError,
)
from .event_registry import contains_sensitive_material
from .queries import PageRequest, TaskSecurityQueryService
from .redaction import normalize_source_ip_masked
from .schemas import (
    ErrorResponse,
    InternalSecurityEvent,
    InternalSecurityEventList,
    InternalStageList,
    InternalTask,
    InternalTaskEventList,
    InternalTaskList,
    as_api_payload,
)
from .security import SecurityAuditService, SecurityEventInput
from .snapshots import SnapshotStore
from .sse import InternalSSEService

VIEW_PERMISSIONS = frozenset({"monitor:observability:view"})
DETAIL_PERMISSIONS = frozenset(
    {"monitor:observability:view", "monitor:observability:detail"}
)
SECURITY_LIST_PERMISSIONS = frozenset(
    {"monitor:observability:view", "monitor:observability:security"}
)
SECURITY_DETAIL_PERMISSIONS = frozenset(
    {
        "monitor:observability:view",
        "monitor:observability:detail",
        "monitor:observability:security",
    }
)
SAFE_RESPONSE_HEADERS = {
    "Cache-Control": "no-store, private",
    "Referrer-Policy": "no-referrer",
    "Vary": "Authorization, X-Observability-Scope",
}
MTLS = {"x-mtls-required": True}
SORT_TASKS = {
    **MTLS,
    "x-fixed-sort": [
        {"field": "createdAt", "direction": "DESC"},
        {"field": "taskId", "direction": "DESC"},
    ],
}
SORT_EVENTS_DESC = {
    **MTLS,
    "x-fixed-sort": [
        {"field": "occurredAt", "direction": "DESC"},
        {"field": "eventId", "direction": "DESC"},
    ],
}
SORT_EVENTS_ASC = {
    **MTLS,
    "x-fixed-sort": [
        {"field": "sequence", "direction": "ASC"},
        {"field": "eventId", "direction": "ASC"},
    ],
}


class InternalObservabilityRoute(APIRoute):
    """Normalize failures without echoing URLs, headers or capabilities."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            request_id = _safe_response_request_id(request.headers.get("X-Request-ID"))

            async def invoke():
                raw_deadline = str(request.headers.get("X-Request-Deadline-Ms") or "")
                if request.url.path.endswith("/events/stream") or not raw_deadline:
                    return await original(request)
                try:
                    deadline_ms = int(raw_deadline)
                except ValueError:
                    return await original(request)
                if deadline_ms < 100 or deadline_ms > 5000:
                    return await original(request)
                async with asyncio.timeout(deadline_ms / 1000):
                    return await original(request)

            try:
                return await invoke()
            except InternalObservabilityError as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content=exc.as_payload(),
                    headers=_response_headers(exc.request_id),
                )
            except TimeoutError:
                return _error_response(
                    503, "OBSERVABILITY_SOURCE_TIMEOUT", request_id, retryable=True
                )
            except DBAPIError:
                return _error_response(
                    503,
                    "OBSERVABILITY_SOURCE_UNAVAILABLE",
                    request_id,
                    retryable=True,
                )
            except RequestValidationError:
                return _error_response(400, "OBSERVABILITY_QUERY_INVALID", request_id)
            except HTTPException as exc:
                code = {
                    400: "OBSERVABILITY_QUERY_INVALID",
                    401: "INTERNAL_AUTHENTICATION_REQUIRED",
                    403: "INTERNAL_SCOPE_FORBIDDEN",
                    404: "OBSERVABILITY_RESOURCE_NOT_FOUND",
                    409: "OBSERVABILITY_CONFLICT",
                    410: "EVENT_STREAM_RESET_REQUIRED",
                    429: "OBSERVABILITY_RATE_LIMITED",
                    503: "OBSERVABILITY_SOURCE_UNAVAILABLE",
                }.get(exc.status_code, "OBSERVABILITY_INTERNAL_ERROR")
                return _error_response(exc.status_code, code, request_id)
            except Exception:
                return _error_response(500, "OBSERVABILITY_INTERNAL_ERROR", request_id)

        return handler


def create_task_security_observability_router(
    *,
    authenticator: HmacInternalAuthenticator,
    snapshots: SnapshotStore,
    security: SecurityAuditService | None = None,
    sse_factory: Callable[[TaskSecurityQueryService], InternalSSEService] | None = None,
) -> APIRouter:
    security = security or SecurityAuditService()
    queries = TaskSecurityQueryService(snapshots, security)
    sse = sse_factory(queries) if sse_factory else InternalSSEService(queries)
    router = APIRouter(
        prefix="/internal/observability/v1",
        route_class=InternalObservabilityRoute,
    )

    async def record_pre_context_rejection(
        failure: InternalObservabilityError,
    ) -> None:
        if failure.status_code not in {401, 403}:
            return
        actor_type = (
            "ANONYMOUS"
            if failure.status_code == 401
            and failure.code
            in {
                "INTERNAL_MTLS_REQUIRED",
                "INTERNAL_SERVICE_TOKEN_INVALID",
            }
            else "UNKNOWN"
        )
        try:
            await security.append(
                SecurityEventInput(
                    audit_action_id="internal_reject_" + uuid.uuid4().hex,
                    service="python-observability",
                    scope_type="SYSTEM",
                    category=(
                        "AUTHENTICATION"
                        if failure.status_code == 401
                        else "AUTHORIZATION"
                    ),
                    action=failure.code,
                    risk_level="HIGH",
                    actor_type=actor_type,
                    missing_context_reason=(
                        "REJECTED_BEFORE_TRUSTED_CONTEXT_RESOLUTION"
                    ),
                    outcome="DENIED",
                    display_code=failure.code,
                    metadata=[
                        {
                            "key": "decision_result",
                            "value": "DENIED",
                            "masked": False,
                        },
                        {
                            "key": "failure_stage",
                            "value": (
                                "AUTHENTICATION"
                                if failure.status_code == 401
                                else "SCOPE_BINDING"
                            ),
                            "masked": False,
                        },
                        {
                            "key": "transport",
                            "value": "INTERNAL_HTTP",
                            "masked": False,
                        },
                    ],
                )
            )
        except Exception:
            # Authentication remains denied even if its audit sink is unavailable.
            return

    async def record_permission_rejection(
        context: InternalAuthContext,
    ) -> None:
        tenant_id = (
            context.current_tenant_id if context.tenant_scope == "CURRENT" else None
        )
        try:
            await security.append(
                SecurityEventInput(
                    audit_action_id=(
                        context.audit_action_id
                        or "permission_reject_" + uuid.uuid4().hex
                    ),
                    access_session_id=context.access_session_id,
                    parent_audit_event_id=context.parent_audit_event_id,
                    service="python-observability",
                    scope_type="TENANT" if tenant_id is not None else "SYSTEM",
                    tenant_id=tenant_id,
                    category="AUTHORIZATION",
                    action="INTERNAL_PERMISSION_DENIED",
                    risk_level="HIGH",
                    actor_type=context.actor_type,
                    actor_id=context.actor_id,
                    subject_type="INTERNAL_OBSERVABILITY_API",
                    missing_context_reason="ROUTE_SUBJECT_NOT_RESOLVED",
                    cross_tenant=context.cross_tenant,
                    reason_code=context.access_reason_code,
                    outcome="DENIED",
                    display_code="INTERNAL_PERMISSION_DENIED",
                    metadata=[
                        {
                            "key": "decision_result",
                            "value": "DENIED",
                            "masked": False,
                        },
                        {
                            "key": "failure_stage",
                            "value": "AUTHORIZATION",
                            "masked": False,
                        },
                        {
                            "key": "resource_type",
                            "value": "INTERNAL_OBSERVABILITY_API",
                            "masked": False,
                        },
                    ],
                    idempotency_key=None,
                )
            )
        except Exception:
            # Permission denial is already fail-closed; never turn this into allow.
            return

    async def authorize(
        request: Request,
        required_permissions: frozenset[str],
        *,
        deadline: bool = True,
    ) -> InternalAuthContext:
        try:
            context = await authenticator.authenticate(request)
        except InternalObservabilityError as failure:
            await record_pre_context_rejection(failure)
            raise
        if deadline:
            _require_deadline(request, context.request_id)
        if not required_permissions.issubset(context.permissions):
            await record_permission_rejection(context)
            raise InternalObservabilityError(
                403, "INTERNAL_SCOPE_FORBIDDEN", context.request_id
            )
        return context

    async def audited(
        context: InternalAuthContext,
        *,
        action: str,
        subject_type: str,
        subject_id: str | None,
        sensitive: bool,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        audit_subject_id = _safe_audit_subject_id(subject_id)
        try:
            value = await operation()
        except ResourceAccessError as exc:
            await security.record_internal_access(
                context,
                action=action,
                subject_type=subject_type,
                subject_id=audit_subject_id,
                sensitive=sensitive,
                decision_result=exc.audit_result,
                error_code=exc.code,
            )
            raise
        except InternalObservabilityError as exc:
            await security.record_internal_access(
                context,
                action=action,
                subject_type=subject_type,
                subject_id=audit_subject_id,
                sensitive=sensitive,
                decision_result=(
                    "DENIED" if exc.status_code in {401, 403} else "FAILURE"
                ),
                error_code=exc.code,
            )
            raise
        except Exception:
            await security.record_internal_access(
                context,
                action=action,
                subject_type=subject_type,
                subject_id=audit_subject_id,
                sensitive=sensitive,
                decision_result="FAILURE",
                error_code="OBSERVABILITY_INTERNAL_ERROR",
            )
            raise
        await security.record_internal_access(
            context,
            action=action,
            subject_type=subject_type,
            subject_id=audit_subject_id,
            sensitive=sensitive,
            decision_result="SUCCESS",
        )
        return value

    @router.get(
        "/tasks",
        tags=["Tasks"],
        summary="查询 Python Task",
        operation_id="internalListTasks",
        response_model=InternalTaskList,
        responses=_responses(400, 401, 403, 429, 500, 503),
        openapi_extra=SORT_TASKS,
    )
    async def list_tasks(
        request: Request,
        from_at: datetime = Query(alias="from"),
        to_at: datetime = Query(alias="to"),
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None, alias="highWatermark", min_length=14, max_length=84
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        retry_token: str | None = Query(
            default=None, alias="retryToken", min_length=16, max_length=4096
        ),
        limit: int = Query(default=100, ge=1, le=200),
        feature_code: str | None = Query(
            default=None, alias="featureCode", max_length=120
        ),
        status: str | None = Query(default=None, max_length=32),
        error_code: str | None = Query(default=None, alias="errorCode", max_length=120),
    ):
        context = await authorize(request, VIEW_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_TASK_LIST",
            subject_type="TASK_LIST",
            subject_id=None,
            sensitive=False,
            operation=lambda: queries.list_tasks(
                context,
                from_at=from_at,
                to_at=to_at,
                snapshot_to=snapshot_to,
                page=_page(high_watermark, cursor, retry_token, limit),
                feature_code=feature_code,
                status=status,
                error_code=error_code,
            ),
        )
        return _success(result, context.request_id)

    @router.get(
        "/tasks/{taskId}",
        tags=["Tasks"],
        summary="获取 Task 详情",
        operation_id="internalGetTask",
        response_model=InternalTask,
        responses=_responses(401, 403, 404, 429, 500, 503),
        openapi_extra={**MTLS, "x-detail-consistency": "LATEST"},
    )
    async def get_task(
        request: Request,
        taskId: str = Path(min_length=1, max_length=80),
    ):
        context = await authorize(request, DETAIL_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_TASK_DETAIL",
            subject_type="TASK",
            subject_id=taskId,
            sensitive=True,
            operation=lambda: queries.get_task(context, taskId),
        )
        return _success(result, context.request_id)

    @router.get(
        "/tasks/{taskId}/timeline",
        tags=["Tasks"],
        summary="获取 Task/Run/Stage 时间线",
        operation_id="internalGetTaskTimeline",
        response_model=InternalTaskEventList,
        responses=_responses(401, 403, 404, 429, 500, 503),
        openapi_extra=SORT_EVENTS_ASC,
    )
    async def task_timeline(
        request: Request,
        taskId: str,
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None, alias="highWatermark", min_length=14, max_length=84
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        retry_token: str | None = Query(
            default=None, alias="retryToken", min_length=16, max_length=4096
        ),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        context = await authorize(request, DETAIL_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_TASK_TIMELINE",
            subject_type="TASK",
            subject_id=taskId,
            sensitive=True,
            operation=lambda: queries.list_task_timeline(
                context,
                task_id=taskId,
                snapshot_to=snapshot_to,
                page=_page(high_watermark, cursor, retry_token, limit),
            ),
        )
        return _success(result, context.request_id)

    @router.get(
        "/task-events",
        tags=["Tasks"],
        summary="全局查询 Task Event，支撑外部统一事件中心",
        operation_id="internalListTaskEvents",
        response_model=InternalTaskEventList,
        responses=_responses(400, 401, 403, 429, 500, 503),
        openapi_extra=SORT_EVENTS_DESC,
    )
    async def list_task_events(
        request: Request,
        from_at: datetime = Query(alias="from"),
        to_at: datetime = Query(alias="to"),
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None, alias="highWatermark", min_length=14, max_length=84
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        retry_token: str | None = Query(
            default=None, alias="retryToken", min_length=16, max_length=4096
        ),
        limit: int = Query(default=100, ge=1, le=200),
        task_id: str | None = Query(default=None, alias="taskId", max_length=80),
        run_id: str | None = Query(default=None, alias="runId", max_length=80),
        stage_id: str | None = Query(default=None, alias="stageId", max_length=80),
        event_type: str | None = Query(default=None, alias="eventType", max_length=80),
        level: Literal["DEBUG", "INFO", "WARN", "ERROR"] | None = None,
        error_code: str | None = Query(default=None, alias="errorCode", max_length=120),
    ):
        context = await authorize(request, VIEW_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_TASK_EVENT_LIST",
            subject_type="TASK_EVENT_LIST",
            subject_id=None,
            sensitive=False,
            operation=lambda: queries.list_task_events(
                context,
                from_at=from_at,
                to_at=to_at,
                snapshot_to=snapshot_to,
                page=_page(high_watermark, cursor, retry_token, limit),
                task_id=task_id,
                run_id=run_id,
                stage_id=stage_id,
                event_type=event_type,
                level=level,
                error_code=error_code,
            ),
        )
        return _success(result, context.request_id)

    @router.get(
        "/runs/{runId}/stages",
        tags=["Tasks"],
        summary="查询 Run Stage",
        operation_id="internalListRunStages",
        response_model=InternalStageList,
        responses=_responses(401, 403, 404, 429, 500, 503),
        openapi_extra=MTLS,
    )
    async def run_stages(request: Request, runId: str):
        context = await authorize(request, DETAIL_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_RUN_STAGES",
            subject_type="RUN",
            subject_id=runId,
            sensitive=True,
            operation=lambda: queries.list_run_stages(context, runId),
        )
        return _success(result, context.request_id)

    @router.get(
        "/runs/{runId}/events",
        tags=["Tasks"],
        summary="查询 Run Task Event",
        operation_id="internalListRunEvents",
        response_model=InternalTaskEventList,
        responses=_responses(401, 403, 404, 429, 500, 503),
        openapi_extra=SORT_EVENTS_ASC,
    )
    async def run_events(
        request: Request,
        runId: str,
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None, alias="highWatermark", min_length=14, max_length=84
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        retry_token: str | None = Query(
            default=None, alias="retryToken", min_length=16, max_length=4096
        ),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        context = await authorize(request, DETAIL_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_RUN_EVENTS",
            subject_type="RUN",
            subject_id=runId,
            sensitive=True,
            operation=lambda: queries.list_run_events(
                context,
                run_id=runId,
                snapshot_to=snapshot_to,
                page=_page(high_watermark, cursor, retry_token, limit),
            ),
        )
        return _success(result, context.request_id)

    @router.get(
        "/runs/{runId}/events/stream",
        tags=["Tasks"],
        summary="Java 订阅单个 Run 增量事件",
        operation_id="internalStreamRunEvents",
        responses=_responses(401, 403, 404, 409, 410, 429, 503),
        openapi_extra=MTLS,
    )
    async def run_event_stream(
        request: Request,
        runId: str,
        last_event_id: str | None = Header(
            default=None, alias="Last-Event-ID", min_length=1, max_length=80
        ),
    ):
        context = await authorize(request, DETAIL_PERMISSIONS, deadline=False)

        async def prepare_stream():
            return await sse.prepare(context, run_id=runId, last_event_id=last_event_id)

        plan = await audited(
            context,
            action="OBSERVABILITY_RUN_EVENT_STREAM",
            subject_type="RUN",
            subject_id=runId,
            sensitive=True,
            operation=prepare_stream,
        )
        response = sse.response(plan)
        response.headers["X-Request-ID"] = context.request_id
        return response

    @router.get(
        "/security-events",
        tags=["Security"],
        summary="查询 Python 安全审计事件",
        operation_id="internalListSecurityEvents",
        response_model=InternalSecurityEventList,
        responses=_responses(400, 401, 403, 429, 500, 503),
        openapi_extra={
            **MTLS,
            "x-fixed-sort": [
                {"field": "occurredAt", "direction": "DESC"},
                {"field": "eventId", "direction": "DESC"},
            ],
        },
    )
    async def list_security_events(
        request: Request,
        from_at: datetime = Query(alias="from"),
        to_at: datetime = Query(alias="to"),
        snapshot_to: datetime = Query(alias="snapshotTo"),
        high_watermark: str | None = Query(
            default=None, alias="highWatermark", min_length=14, max_length=84
        ),
        cursor: str | None = Query(default=None, min_length=16, max_length=4096),
        retry_token: str | None = Query(
            default=None, alias="retryToken", min_length=16, max_length=4096
        ),
        limit: int = Query(default=100, ge=1, le=200),
        scope_type: Literal["TENANT", "SYSTEM"] | None = Query(
            default=None, alias="scopeType"
        ),
        category: str | None = Query(default=None, max_length=80),
        action: str | None = Query(default=None, max_length=120),
        risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = Query(
            default=None, alias="riskLevel"
        ),
        actor_id: str | None = Query(default=None, alias="actorId", max_length=120),
        outcome: Literal[
            "SUCCESS",
            "FAILURE",
            "DENIED",
            "CANCELLED",
            "TIMEOUT",
            "PARTIAL",
            "UNKNOWN",
            "ABANDONED",
        ]
        | None = None,
        subject_type: str | None = Query(
            default=None, alias="subjectType", max_length=80
        ),
        subject_id: str | None = Query(default=None, alias="subjectId", max_length=120),
        reason_code: str | None = Query(
            default=None, alias="reasonCode", max_length=80
        ),
        source_ip_masked: str | None = Query(
            default=None, alias="sourceIpMasked", max_length=64
        ),
        cross_tenant: bool | None = Query(default=None, alias="crossTenant"),
        audit_action_id: str | None = Query(
            default=None, alias="auditActionId", max_length=96
        ),
        access_session_id: str | None = Query(
            default=None, alias="accessSessionId", max_length=96
        ),
        audit_layer: Literal["JAVA_GATEWAY", "PYTHON_EXECUTION"] | None = Query(
            default=None, alias="auditLayer"
        ),
    ):
        context = await authorize(request, SECURITY_LIST_PERMISSIONS)
        try:
            source_ip_masked = normalize_source_ip_masked(source_ip_masked)
        except ValueError:
            raise InternalObservabilityError(
                400, "OBSERVABILITY_QUERY_INVALID", context.request_id
            ) from None
        filters = {
            "scopeType": scope_type,
            "category": category,
            "action": action,
            "riskLevel": risk_level,
            "actorId": actor_id,
            "outcome": outcome,
            "subjectType": subject_type,
            "subjectId": subject_id,
            "reasonCode": reason_code,
            "sourceIpMasked": source_ip_masked,
            "crossTenant": cross_tenant,
            "auditActionId": audit_action_id,
            "accessSessionId": access_session_id,
            "auditLayer": audit_layer,
        }
        result = await audited(
            context,
            action="OBSERVABILITY_SECURITY_EVENT_LIST",
            subject_type="SECURITY_EVENT_LIST",
            subject_id=None,
            sensitive=False,
            operation=lambda: queries.list_security_events(
                context,
                from_at=from_at,
                to_at=to_at,
                snapshot_to=snapshot_to,
                page=_page(high_watermark, cursor, retry_token, limit),
                filters=filters,
            ),
        )
        return _success(result, context.request_id)

    @router.get(
        "/security-events/{eventId}",
        tags=["Security"],
        summary="查询 Python 安全事件详情",
        operation_id="internalGetSecurityEvent",
        response_model=InternalSecurityEvent,
        responses=_responses(401, 403, 404, 500, 503),
        openapi_extra=MTLS,
    )
    async def get_security_event(request: Request, eventId: str):
        context = await authorize(request, SECURITY_DETAIL_PERMISSIONS)
        result = await audited(
            context,
            action="OBSERVABILITY_SECURITY_EVENT_DETAIL",
            subject_type="SECURITY_EVENT",
            subject_id=eventId,
            sensitive=True,
            operation=lambda: queries.get_security_event(context, eventId),
        )
        return _success(result, context.request_id)

    return router


def _responses(*statuses: int) -> dict[int, dict[str, Any]]:
    descriptions = {
        400: "参数、时间窗、快照、高水位或游标无效",
        401: "mTLS、Service JWT 或 Scope Token 无效",
        403: "环境、权限、Path 绑定或租户范围不允许",
        404: "资源不存在或对 Scope 不可见",
        409: "Last-Event-ID 超出可重放窗口",
        410: "事件已清理、无法定位或 Run 已归档",
        429: "查询预算、速率或快照容量限制",
        500: "Python 内部错误",
        503: "来源不可用",
    }
    return {
        status: {"model": ErrorResponse, "description": descriptions[status]}
        for status in statuses
    }


def _page(
    high_watermark: str | None,
    cursor: str | None,
    retry_token: str | None,
    limit: int,
) -> PageRequest:
    return PageRequest(
        high_watermark=high_watermark,
        cursor=cursor,
        retry_token=retry_token,
        limit=limit,
    )


def _require_deadline(request: Request, request_id: str) -> int:
    raw = str(request.headers.get("X-Request-Deadline-Ms") or "")
    try:
        value = int(raw)
    except ValueError:
        raise InternalObservabilityError(
            400, "OBSERVABILITY_DEADLINE_INVALID", request_id
        ) from None
    if value < 100 or value > 5000:
        raise InternalObservabilityError(
            400, "OBSERVABILITY_DEADLINE_INVALID", request_id
        )
    return value


def _success(value: Any, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=as_api_payload(value),
        headers=_response_headers(request_id),
    )


def _error_response(
    status: int,
    code: str,
    request_id: str,
    *,
    retryable: bool | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "requestId": request_id,
            "retryable": status in {409, 429, 503} if retryable is None else retryable,
        },
        headers=_response_headers(request_id),
    )


def _safe_audit_subject_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 120:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return None if contains_sensitive_material(value) else value


def _safe_response_request_id(value: object) -> str:
    normalized = normalize_request_id(value)
    if normalized is not None:
        return normalized
    return (
        "req_missing"
        if not isinstance(value, str) or not value.strip()
        else "req_invalid"
    )


def _response_headers(request_id: str) -> dict[str, str]:
    return {"X-Request-ID": request_id, **SAFE_RESPONSE_HEADERS}
