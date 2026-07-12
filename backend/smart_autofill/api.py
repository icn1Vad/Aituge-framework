from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from .documents import SmartFillDocumentStore
from .extraction import build_extraction_input


def create_smart_autofill_router(store: SmartFillDocumentStore | None = None) -> APIRouter:
    router = APIRouter(prefix="/smart-autofill", tags=["smart-autofill"])
    document_store = store or SmartFillDocumentStore()

    @router.post("/documents")
    async def upload_documents(files: list[UploadFile] = File(...)):
        try:
            documents = [
                document_store.ingest(file.filename or "upload", await file.read())
                for file in files
            ]
            return {"documents": documents, "document_ids": [item["document_id"] for item in documents]}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/documents/{document_id}")
    async def get_document(document_id: str, include_chunks: bool = True):
        try:
            parsed = document_store.get(document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found.") from exc
        return parsed if include_chunks else document_store.summary(parsed)

    @router.post("/extraction-input")
    async def extraction_input(document_ids: list[str]):
        missing = []
        for document_id in document_ids:
            try:
                document_store.get(document_id)
            except KeyError:
                missing.append(document_id)
        if missing:
            raise HTTPException(status_code=404, detail=f"Documents not found: {', '.join(missing)}")
        try:
            return build_extraction_input(document_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
