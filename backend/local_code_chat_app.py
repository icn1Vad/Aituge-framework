"""Simple chat app with the local Python runtime tool enabled."""

from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI, File, HTTPException, UploadFile

from backend.simple_chat_app import create_app as create_simple_chat_app
from backend.data.RAG.tool_retrieval import LocalRagStore, ToolRetrievalRAG

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db.db_context import create_db_session, init_db
from tool import ToolBundle
from tool.registry import (
    ToolProviderConfig,
    create_enabled_tool_bundle,
    get_default_tool_list,
)


LOCAL_PYTHON_ARTIFACT_DIR = (
    Path(__file__).resolve().parent / "tool" / "local_runtime" / "artifacts"
)
DEFAULT_RAG_PDF_PATH = (
    Path(__file__).resolve().parents[2]
    / "兼用_原02_致远互联：北京致远互联软件股份有限公司内部审计制度.pdf"
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
    async def tool_provider(_request):
        local_python_bundle = get_default_tool_list().create_bundle(
            ToolProviderConfig(
                tool_name="code_interpreter",
                provider="local_python",
                config={
                    "timeout_seconds": 20,
                    "max_output_chars": 50_000,
                    "work_dir": LOCAL_PYTHON_ARTIFACT_DIR,
                    "artifact_base_url": "/tool-artifacts/local-python",
                    "keep_work_dir": True,
                },
            )
        )

        async with create_db_session() as session:
            db_tool_bundle = await create_enabled_tool_bundle(session)

        rag_bundle = _create_local_rag_bundle()

        return ToolBundle.combine([local_python_bundle, db_tool_bundle, rag_bundle])

    @asynccontextmanager
    async def lifespan(_app):
        await init_db()
        yield

    app = create_simple_chat_app(tool_provider=tool_provider, lifespan=lifespan)

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

    return app
