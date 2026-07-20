from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHttpException

from db.db_context import init_db

from translation_service.application.service import (
    FileTranslationOptions,
    TranslationApplicationService,
)
from translation_service.config import Settings
from translation_service.context import (
    InternalRequestContext,
    require_internal_context,
)
from translation_service.domain.models import (
    GlossaryEntry,
    OfficeOutputFormat,
    PdfOutputMode,
    TextTranslationRequest,
    TranslationLanguage,
)
from translation_service.errors import TranslationError
from translation_service.infrastructure.babeldoc import BabelDocClient
from translation_service.language.detector import LanguageDetector
from translation_service.model.gateway import BaseModelGateway
from translation_service.model.translator import BaseModelTranslator
from translation_service.processors.docx import DocxTranslator
from translation_service.processors.file_safety import clean_all_temp_directories
from translation_service.processors.libreoffice import LibreOfficeConverter
from translation_service.task.tracker import (
    TranslationTaskTracker,
    register_translation_task_types,
)

logger = logging.getLogger(__name__)
_GLOSSARY_ADAPTER = TypeAdapter(list[GlossaryEntry])


def _success(data: object, request_id: str, *, status_code: int = 200) -> JSONResponse:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    elif isinstance(data, list):
        data = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in data
        ]
    return JSONResponse(
        {"success": True, "data": data, "request_id": request_id},
        status_code=status_code,
    )


def _request_id(request: Request) -> str:
    return request.state.request_id


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_settings.prepare_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        register_translation_task_types()
        await init_db()
        tracker = TranslationTaskTracker(resolved_settings)
        await tracker.interrupt_active_tasks()
        clean_all_temp_directories(resolved_settings.temp_root)
        model_gateway = BaseModelGateway(resolved_settings)
        model_translator = BaseModelTranslator(resolved_settings, model_gateway)
        application = TranslationApplicationService(
            settings=resolved_settings,
            tracker=tracker,
            model_translator=model_translator,
            language_detector=LanguageDetector(resolved_settings, model_gateway),
            docx_translator=DocxTranslator(model_translator),
            office_converter=LibreOfficeConverter(resolved_settings),
            babeldoc_client=BabelDocClient(resolved_settings),
        )
        app.state.settings = resolved_settings
        app.state.tracker = tracker
        app.state.translation_service = application
        try:
            yield
        finally:
            await application.shutdown()

    app = FastAPI(
        title="AI-tuge Translation Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        incoming = request.headers.get("X-Request-Id", "").strip()
        request.state.request_id = incoming[:128] if incoming else uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(TranslationError)
    async def handle_translation_error(
        request: Request, exc: TranslationError
    ) -> JSONResponse:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
                "request_id": _request_id(request),
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Request parameters are invalid",
                    "retryable": False,
                },
                "request_id": _request_id(request),
            },
            status_code=422,
        )

    @app.exception_handler(StarletteHttpException)
    async def handle_http_error(
        request: Request, exc: StarletteHttpException
    ) -> JSONResponse:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "HTTP_ERROR",
                    "message": str(exc.detail),
                    "retryable": False,
                },
                "request_id": _request_id(request),
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled translation request failure", exc_info=exc)
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected translation service failure",
                    "retryable": True,
                },
                "request_id": _request_id(request),
            },
            status_code=500,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/translations/text")
    async def translate_text(
        request: Request,
        payload: TextTranslationRequest,
        context: Annotated[
            InternalRequestContext, Depends(require_internal_context)
        ],
    ) -> JSONResponse:
        data = await _service(request).translate_text(context, payload)
        return _success(data, _request_id(request))

    @app.post("/v1/translations/files")
    async def translate_file(
        request: Request,
        file: Annotated[UploadFile, File()],
        source_language: Annotated[TranslationLanguage, Form()],
        target_language: Annotated[TranslationLanguage, Form()],
        context: Annotated[
            InternalRequestContext, Depends(require_internal_context)
        ],
        pdf_output_mode: Annotated[PdfOutputMode, Form()] = PdfOutputMode.MONO,
        office_output_format: Annotated[
            OfficeOutputFormat, Form()
        ] = OfficeOutputFormat.SOURCE,
        glossary_json: Annotated[str, Form()] = "[]",
    ) -> JSONResponse:
        glossary = _parse_glossary(glossary_json)
        data = await _service(request).submit_file(
            context=context,
            upload=file,
            options=FileTranslationOptions(
                source_language=source_language,
                target_language=target_language,
                pdf_output_mode=pdf_output_mode,
                office_output_format=office_output_format,
                glossary=glossary,
            ),
        )
        return _success(data, _request_id(request), status_code=202)

    @app.get("/v1/translations/tasks/{task_id}")
    async def get_task(
        request: Request,
        task_id: str,
        context: Annotated[
            InternalRequestContext, Depends(require_internal_context)
        ],
    ) -> JSONResponse:
        return _success(
            await _service(request).get_task(task_id, context),
            _request_id(request),
        )

    @app.get("/v1/translations/tasks/{task_id}/artifacts")
    async def list_artifacts(
        request: Request,
        task_id: str,
        context: Annotated[
            InternalRequestContext, Depends(require_internal_context)
        ],
    ) -> JSONResponse:
        return _success(
            await _service(request).get_artifacts(task_id, context),
            _request_id(request),
        )

    @app.get("/v1/translations/artifacts/{artifact_id}/content")
    async def download_artifact(
        request: Request,
        artifact_id: str,
        context: Annotated[
            InternalRequestContext, Depends(require_internal_context)
        ],
    ) -> FileResponse:
        path, artifact = await request.app.state.tracker.resolve_owned_artifact(
            artifact_id, context
        )
        if not path.is_file():
            raise TranslationError(
                "ARTIFACT_NOT_FOUND",
                "Translation artifact file was not found",
                status_code=404,
            )
        return FileResponse(
            path,
            media_type=artifact.mime_type,
            filename=artifact.file_name,
            headers={
                "X-Artifact-Sha256": artifact.sha256,
                "X-Artifact-Size": str(artifact.size),
                "X-Request-Id": _request_id(request),
            },
        )

    return app


def _service(request: Request) -> TranslationApplicationService:
    return request.app.state.translation_service


def _parse_glossary(value: str) -> list[GlossaryEntry]:
    if len(value) > 50_000:
        raise TranslationError("GLOSSARY_TOO_LARGE", "Glossary JSON is too large")
    try:
        payload = json.loads(value)
        glossary = _GLOSSARY_ADAPTER.validate_python(payload)
        if len(glossary) > 100:
            raise TranslationError(
                "GLOSSARY_TOO_LARGE", "Glossary cannot contain more than 100 entries"
            )
        return glossary
    except (json.JSONDecodeError, ValidationError) as exc:
        raise TranslationError(
            "INVALID_GLOSSARY",
            "Glossary must be a JSON array of source/target entries",
        ) from exc
