from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from qianxuesen_mentor.config import Settings, get_settings
from qianxuesen_mentor.errors import QianXuesenError
from qianxuesen_mentor.schemas import ChatRequest, RetrievalRequest, SqlRequest
from qianxuesen_mentor.service import MentorService


def create_app(settings: Settings | None = None, service: MentorService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if application.state.service is not None:
            await asyncio.to_thread(application.state.service.bootstrap)
        yield

    app = FastAPI(title="钱学森导师服务", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.state.service = service

    @app.exception_handler(QianXuesenError)
    async def handle_domain_error(_request: Request, exc: QianXuesenError):
        return JSONResponse(status_code=exc.status_code, content={
            "success": False, "error": exc.code, "detail": str(exc), "details": exc.details,
        })

    @app.get("/health")
    async def health():
        return {"success": True, "data": await asyncio.to_thread(_service, app, "health")}

    @app.get("/v1/files")
    async def files():
        return {"success": True, "data": await asyncio.to_thread(_service, app, "list_files")}

    @app.get("/v1/files/{file_id}/content")
    async def file_content(file_id: str):
        data = await asyncio.to_thread(_service, app, "file_content", file_id)
        return FileResponse(data["path"], media_type="application/pdf", filename=data["name"],
                            content_disposition_type="inline")

    @app.get("/v1/files/{file_id}/chunks")
    async def chunks(file_id: str, limit: int = Query(default=10, ge=1, le=100),
                     offset: int = Query(default=0, ge=0)):
        data = await asyncio.to_thread(_service, app, "list_chunks", file_id, limit=limit, offset=offset)
        return {"success": True, "data": data}

    @app.post("/v1/retrieval/search")
    async def search(payload: RetrievalRequest):
        data = await asyncio.to_thread(_service, app, "search", payload.query,
                                       top_k=payload.top_k, retrieval_mode=payload.retrieval_mode)
        return {"success": True, "data": data}

    @app.post("/v1/query/sql")
    async def sql(payload: SqlRequest):
        data = await asyncio.to_thread(_service, app, "execute_sql", payload.question, payload.sql)
        return {"success": True, "data": data}

    @app.post("/v1/chat/stream")
    async def chat_stream(payload: ChatRequest):
        service_instance = _service_instance(app)

        async def events():
            try:
                async for event_type, data in service_instance.answering.stream(
                    payload.question,
                    history=payload.history,
                    top_k=payload.top_k,
                ):
                    yield _sse_event(event_type, data)
            except QianXuesenError as exc:
                yield _sse_event("error", {
                    "errorCode": exc.code,
                    "errorMessage": str(exc),
                    "retryable": exc.status_code >= 500,
                })
            except Exception:
                yield _sse_event("error", {
                    "errorCode": "qxs_answer_failed",
                    "errorMessage": "导师回答生成失败，请稍后重试",
                    "retryable": True,
                })

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return app


def _service(app: FastAPI, method: str, *args, **kwargs):
    return getattr(_service_instance(app), method)(*args, **kwargs)


def _service_instance(app: FastAPI):
    if app.state.service is None:
        app.state.service = MentorService(app.state.settings)
        app.state.service.bootstrap()
    return app.state.service


def _sse_event(event_type: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n"


app = create_app()
