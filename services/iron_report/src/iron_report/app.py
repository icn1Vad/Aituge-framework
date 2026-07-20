from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.local_code_chat_app import RAG_STORE, create_app as create_framework_app
from backend.simple_chat_app import LOCAL_PYTHON_ARTIFACT_DIR, LOCAL_PYTHON_WORK_DIR
from scheduling.scheduler import SchedulingRuntimeOptions
from sqlmodel import select

from common.encrypt_utils import encrypt_key
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from db.models.llm import LlmModelEntity
from tool.registry.models import ToolConfigEntity

from iron_report.api import create_router
from iron_report.config import Settings, get_settings
from iron_report.errors import IronReportError
from iron_report.service import IronReportService


def create_app(settings: Settings | None = None):
    configured = settings or get_settings()
    app = create_framework_app()
    options = SchedulingRuntimeOptions(
        local_python_artifact_dir=LOCAL_PYTHON_ARTIFACT_DIR,
        local_python_work_dir=LOCAL_PYTHON_WORK_DIR,
        rag_store=RAG_STORE,
    )
    service = IronReportService(configured, options)
    app.state.iron_report_service = service
    configured.runtime_root.mkdir(parents=True, exist_ok=True)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        configured.runtime_root.mkdir(parents=True, exist_ok=True)
        service.data.health()
        async with original_lifespan(application):
            await _configure_model(configured)
            await _configure_optional_web_search(configured)
            yield

    app.router.lifespan_context = lifespan
    app.include_router(create_router(service))

    @app.exception_handler(IronReportError)
    async def iron_report_error_handler(request: Request, exc: IronReportError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": exc.code,
                "detail": str(exc),
                "details": exc.details,
                "retryable": exc.retryable,
                "request_id": _request_id(request),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/v1/iron-reports"):
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "IRON_REPORT_VALIDATION_ERROR",
                "detail": "请求参数校验失败",
                "details": {"errors": jsonable_encoder(exc.errors())},
                "retryable": False,
                "request_id": _request_id(request),
            },
        )

    return app


async def _configure_model(settings: Settings) -> None:
    model_name = settings.model_name.strip()
    base_url = settings.model_base_url.strip()
    api_key = settings.model_api_key.strip()
    if not model_name or not base_url or not api_key:
        raise RuntimeError(
            "IRON_REPORT_MODEL_NAME, IRON_REPORT_MODEL_BASE_URL and IRON_REPORT_MODEL_API_KEY are required"
        )
    async with create_db_session() as session:
        result = await session.exec(
            select(LlmModelEntity).where(
                LlmModelEntity.tenant_id == DEFAULT_TENANT_ID,
                LlmModelEntity.model_id == settings.model_id,
            )
        )
        entity = result.first()
        if entity is None:
            entity = LlmModelEntity(
                tenant_id=DEFAULT_TENANT_ID,
                model_id=settings.model_id,
            )
        entity.base_url = base_url
        entity.model = model_name
        entity.model_name = model_name
        entity.provider_name = settings.model_provider.strip() or "openai_like"
        entity.source = entity.provider_name
        entity.enabled = True
        entity.vision_support = False
        entity.enable_thinking = False
        entity.context_window = settings.model_context_window
        entity.max_tokens = settings.model_max_tokens
        entity.encrypted_api_key = encrypt_key(api_key)
        session.add(entity)
        await session.commit()


async def _configure_optional_web_search(settings: Settings) -> None:
    endpoint = settings.search_endpoint.strip()
    api_key = settings.search_api_key.strip()
    if not endpoint or not api_key:
        return
    async with create_db_session() as session:
        result = await session.exec(
            select(ToolConfigEntity).where(
                ToolConfigEntity.tenant_id == DEFAULT_TENANT_ID,
                ToolConfigEntity.tool_name == "web_search",
                ToolConfigEntity.provider == "aliyun",
            )
        )
        entity = result.first()
        config_json = json.dumps(
            {"endpoint": endpoint, "search_count": settings.search_count},
            ensure_ascii=True,
            sort_keys=True,
        )
        secrets_json = encrypt_key(json.dumps({"api_key": api_key}, ensure_ascii=True))
        if entity is None:
            entity = ToolConfigEntity(
                tenant_id=DEFAULT_TENANT_ID,
                tool_name="web_search",
                provider="aliyun",
                enabled=True,
                config_json=config_json,
                encrypted_secrets_json=secrets_json,
            )
        else:
            entity.enabled = True
            entity.config_json = config_json
            entity.encrypted_secrets_json = secrets_json
        session.add(entity)
        await session.commit()


def _request_id(request: Request) -> str:
    value = (request.headers.get("X-Request-Id") or "").strip()
    return value[:128] if value else uuid.uuid4().hex
