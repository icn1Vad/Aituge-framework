"""Simple chat app with the local Python runtime tool enabled."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from backend.simple_chat_app import (
    LOCAL_PYTHON_ARTIFACT_DIR,
    LOCAL_PYTHON_WORK_DIR,
    create_app as create_simple_chat_app,
)
from backend.data.RAG.tool_retrieval import LocalRagStore, ToolRetrievalRAG

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db.db_context import create_db_session, init_db
from capability_mount import mount_capabilities_from_env
from scheduling.agent_registry import ensure_default_agent_profiles
from scheduling.api import create_scheduling_router
from scheduling.scheduler import SchedulingRuntimeOptions
from skill import ensure_default_skill_packages
from task_manager import create_task_manager_router
from tool import ToolBundle
from tool.registry import ToolManager
from backend.revision_llm_api import create_revision_llm_router
from contract.api.app import create_app as create_contract_app
from contract.persistence.postgres.migrate import run_migrations as run_contract_migrations


TASK_MEMORY_TEST_DIR = Path(__file__).resolve().parents[1] / "frontend" / "task-memory-test"
DEFAULT_RAG_PDF_PATH = (
    BACKEND_DIR
    / "data"
    / "RAG"
    / "tool_retrieval"
    / "store"
    / "uploads"
    / "1.pdf"
)
RAG_STORE = LocalRagStore()


def _rag_status_payload() -> dict:
    knowledgebases, files, chunks = RAG_STORE.load_models()
    files_by_kb = {}
    chunks_by_file = {}
    for file in files:
        files_by_kb.setdefault(file.kb_id, []).append(file)
    for chunk in chunks:
        chunks_by_file.setdefault(chunk.file_id, []).append(chunk)

    return {
        "ok": True,
        "knowledgebase_count": len(knowledgebases),
        "file_count": len(files),
        "chunk_count": len(chunks),
        "retrieval": {
            "mode": "keyword",
            "backend": "InMemoryRagBackend",
            "embedding": None,
            "rerank": None,
            "tools_per_kb": ["search", "catalog", "grep", "fetch"],
        },
        "knowledgebases": [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description,
                "tool_names": [
                    f"search-knowledgebase-{kb.id[:10]}",
                    f"catalog-{kb.id[:8]}",
                    f"grep-{kb.id[:8]}",
                    f"fetch-{kb.id[:8]}",
                ],
                "file_count": len(files_by_kb.get(kb.id, [])),
                "chunk_count": sum(
                    len(chunks_by_file.get(file.id, []))
                    for file in files_by_kb.get(kb.id, [])
                ),
                "files": [
                    {
                        "id": file.id,
                        "file_name": file.file_name,
                        "title": file.title,
                        "source_url": file.source_url,
                        "source_path": dict(file.metadata).get("source_path"),
                        "status": file.status,
                        "chunk_count": len(chunks_by_file.get(file.id, [])),
                        "chunks": [
                            {
                                "id": chunk.id,
                                "index": chunk.index,
                                "char_count": len(chunk.text),
                                "preview": chunk.text[:160].replace("\n", " "),
                            }
                            for chunk in sorted(
                                chunks_by_file.get(file.id, []),
                                key=lambda item: item.index,
                            )[:5]
                        ],
                    }
                    for file in files_by_kb.get(kb.id, [])
                ],
            }
            for kb in knowledgebases
        ],
    }


def _create_local_rag_bundle() -> ToolBundle:
    knowledgebases, files, chunks = RAG_STORE.load_models()
    if not knowledgebases:
        return ToolBundle.empty()
    rag_tools = ToolRetrievalRAG(
        knowledgebases=knowledgebases,
        files=files,
        chunks=chunks,
    ).create_tools()
    return ToolBundle.from_tools(rag_tools)


def create_app() -> FastAPI:
    embedded_contract_app = (
        create_contract_app()
        if os.getenv("CONTRACT_EMBEDDED_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        else None
    )

    async def tool_provider(_request):
        tool_bundle = await ToolManager(
            local_python_work_dir=LOCAL_PYTHON_WORK_DIR,
        ).create_bundle(["code_interpreter", "enabled_db_tools"])
        rag_bundle = _create_local_rag_bundle()

        return ToolBundle.combine([tool_bundle, rag_bundle])

    @asynccontextmanager
    async def lifespan(_app):
        await init_db()
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            await ensure_default_agent_profiles(session)
            await mount_capabilities_from_env(session=session)
        if embedded_contract_app is None:
            yield
            return
        await asyncio.to_thread(
            run_contract_migrations,
            embedded_contract_app.state.settings,
        )
        async with embedded_contract_app.router.lifespan_context(
            embedded_contract_app
        ):
            yield

    app = create_simple_chat_app(tool_provider=tool_provider, lifespan=lifespan)
    scheduling_options = SchedulingRuntimeOptions(
        local_python_artifact_dir=LOCAL_PYTHON_ARTIFACT_DIR,
        local_python_work_dir=LOCAL_PYTHON_WORK_DIR,
        rag_store=RAG_STORE,
    )
    app.include_router(create_scheduling_router(scheduling_options))
    app.include_router(create_task_manager_router(scheduling_options))
    app.include_router(create_revision_llm_router())
    app.mount(
        "/task-memory-test",
        StaticFiles(directory=TASK_MEMORY_TEST_DIR, html=True),
        name="task-memory-test",
    )
    @app.get("/rag/status")
    async def rag_status():
        return _rag_status_payload()

    @app.post("/rag/ingest-default")
    async def ingest_default_pdf():
        try:
            return RAG_STORE.ingest_pdf(
                DEFAULT_RAG_PDF_PATH,
                kb_name="kb_1",
                kb_description="北京致远互联软件股份有限公司内部审计制度。",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/rag/ingest-upload")
    async def ingest_uploaded_pdf(file: UploadFile = File(...)):
        try:
            content = await file.read()
            return await RAG_STORE.ingest_upload(file.filename or "upload.pdf", content)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if embedded_contract_app is not None:
        app.mount("/", embedded_contract_app, name="contract")

    return app
