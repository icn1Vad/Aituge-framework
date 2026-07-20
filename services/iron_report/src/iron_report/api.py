from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from iron_report.schemas import IronReportCreateRequest, ReportType
from iron_report.service import IronReportService, RequestContext


class MarketDataToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: ReportType
    report_date: str


class ResearchToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=400)
    report_date: str


def create_router(service: IronReportService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request):
        data = service.data.health()
        data.update(
            {
                "service": "iron-report",
                "modelConfigured": all(
                    (
                        service.settings.model_id,
                        service.settings.model_name,
                        service.settings.model_base_url,
                        service.settings.model_api_key,
                    )
                ),
                "searchConfigured": bool(
                    service.settings.search_endpoint and service.settings.search_api_key
                ),
                "internalTokenConfigured": bool(service.settings.internal_token),
                "requestId": _request_id(request),
            }
        )
        return {"success": True, "data": data}

    @router.post("/v1/iron-reports", status_code=status.HTTP_202_ACCEPTED)
    async def create_report(
        request: Request,
        payload: IronReportCreateRequest = Body(...),
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
        x_user_id: Annotated[str, Header(alias="X-User-Id")] = "",
        x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "",
        x_dept_id: Annotated[str, Header(alias="X-Dept-Id")] = "",
        x_internal_token: Annotated[str, Header(alias="X-Internal-Token")] = "",
    ):
        service.verify_internal_token(x_internal_token)
        context = _context(request, x_user_id, x_tenant_id, x_dept_id)
        data = await service.create_report(context, payload, idempotency_key)
        return _success(
            request,
            data.model_dump(mode="json"),
            status_code=status.HTTP_202_ACCEPTED,
        )

    @router.get("/v1/iron-reports/{report_id}")
    async def get_report(
        report_id: str,
        request: Request,
        x_user_id: Annotated[str, Header(alias="X-User-Id")] = "",
        x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "",
        x_dept_id: Annotated[str, Header(alias="X-Dept-Id")] = "",
        x_internal_token: Annotated[str, Header(alias="X-Internal-Token")] = "",
    ):
        service.verify_internal_token(x_internal_token)
        data = await service.get_status(_context(request, x_user_id, x_tenant_id, x_dept_id), report_id)
        return _success(request, data.model_dump(mode="json"))

    @router.get("/v1/iron-reports/{report_id}/result")
    async def get_result(
        report_id: str,
        request: Request,
        x_user_id: Annotated[str, Header(alias="X-User-Id")] = "",
        x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "",
        x_dept_id: Annotated[str, Header(alias="X-Dept-Id")] = "",
        x_internal_token: Annotated[str, Header(alias="X-Internal-Token")] = "",
    ):
        service.verify_internal_token(x_internal_token)
        data = await service.get_result(_context(request, x_user_id, x_tenant_id, x_dept_id), report_id)
        return _success(request, data)

    @router.get("/v1/iron-reports/{report_id}/artifacts")
    async def list_artifacts(
        report_id: str,
        request: Request,
        x_user_id: Annotated[str, Header(alias="X-User-Id")] = "",
        x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "",
        x_dept_id: Annotated[str, Header(alias="X-Dept-Id")] = "",
        x_internal_token: Annotated[str, Header(alias="X-Internal-Token")] = "",
    ):
        service.verify_internal_token(x_internal_token)
        rows = await service.list_artifacts(_context(request, x_user_id, x_tenant_id, x_dept_id), report_id)
        return _success(request, [item.model_dump(mode="json") for item in rows])

    @router.get("/v1/iron-reports/{report_id}/artifacts/{artifact_id}/content")
    async def get_artifact_content(
        report_id: str,
        artifact_id: str,
        request: Request,
        x_user_id: Annotated[str, Header(alias="X-User-Id")] = "",
        x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "",
        x_dept_id: Annotated[str, Header(alias="X-Dept-Id")] = "",
        x_internal_token: Annotated[str, Header(alias="X-Internal-Token")] = "",
    ):
        service.verify_internal_token(x_internal_token)
        artifact = await service.get_artifact_content(
            _context(request, x_user_id, x_tenant_id, x_dept_id), report_id, artifact_id
        )
        return FileResponse(
            artifact.path,
            media_type=artifact.mime_type,
            filename=artifact.name,
            content_disposition_type="attachment",
            headers={
                "X-Artifact-SHA-256": artifact.sha256,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    @router.post("/v1/internal/iron-report/data", include_in_schema=False)
    async def market_data(payload: MarketDataToolInput):
        report_date = _parse_tool_date(payload.report_date)
        return {"success": True, "data": service.data.build_agent_snapshot(payload.report_type, report_date)}

    @router.post("/v1/internal/iron-report/research", include_in_schema=False)
    async def research(payload: ResearchToolInput):
        report_date = _parse_tool_date(payload.report_date)
        return {"success": True, "data": await service.research(report_date, payload.query)}

    @router.post("/v1/internal/iron-report/task-result", include_in_schema=False)
    async def accept_task_result(
        request: Request,
        payload: dict = Body(...),
        callback_token: str = Query(default=""),
    ):
        service.verify_internal_token(callback_token)
        data = await service.accept_task_result(payload)
        return _success(request, data)

    return router


def _context(request: Request, user_id: str, tenant_id: str, dept_id: str) -> RequestContext:
    from iron_report.errors import IronReportError

    if not user_id.strip() or not tenant_id.strip():
        raise IronReportError(
            "IRON_REPORT_CONTEXT_REQUIRED",
            "X-User-Id 和 X-Tenant-Id 不能为空",
            status_code=400,
        )
    return RequestContext(
        user_id=user_id.strip(),
        tenant_id=tenant_id.strip(),
        dept_id=dept_id.strip(),
        request_id=_request_id(request),
    )


def _parse_tool_date(value: str) -> date:
    from iron_report.errors import IronReportError

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IronReportError(
            "IRON_REPORT_DATE_INVALID", "report_date 必须为 ISO 日期", status_code=422
        ) from exc


def _request_id(request: Request) -> str:
    value = request.headers.get("X-Request-Id", "").strip()
    return value[:128] if value else uuid.uuid4().hex


def _success(request: Request, data, *, status_code: int = status.HTTP_200_OK):
    return JSONResponse(
        status_code=status_code,
        content={"success": True, "data": data, "request_id": _request_id(request)},
    )
