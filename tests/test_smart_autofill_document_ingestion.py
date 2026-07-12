from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from smart_autofill import SmartFillDocumentStore, create_smart_autofill_router


PDF_FIXTURE = Path(r"E:\MyProjects\proofreading\EPC合同  (Executed 31082023).pdf")
DOCX_FIXTURE = Path(r"E:\MyProjects\方案\03-2关于航天长征化学工程股份有限公司收购航天氢能有限公司股权的可行性研究报告（公开）.docx")


@pytest.mark.parametrize(
    ("path", "extension"),
    [(PDF_FIXTURE, ".pdf"), (DOCX_FIXTURE, ".docx")],
)
def test_fixed_benchmark_documents_produce_anchored_content(tmp_path, path, extension) -> None:
    store = SmartFillDocumentStore(tmp_path / "documents")
    summary = store.ingest(path.name, path.read_bytes())
    parsed = store.get(summary["document_id"])

    assert summary["extension"] == extension
    assert summary["quality"]["status"] == "usable"
    assert summary["text_chars"] > 10_000
    assert parsed["chunks"]
    assert all(chunk["document_id"] == summary["document_id"] for chunk in parsed["chunks"])
    assert all("char_start" in chunk["anchor"] for chunk in parsed["chunks"])
    if extension == ".pdf":
        assert summary["page_count"] == 155
        assert all("page" in chunk["anchor"] for chunk in parsed["chunks"])
    else:
        assert summary["paragraph_count"] >= 150
        assert summary["table_count"] == 17
        assert any(chunk["kind"] == "table" and chunk["anchor"]["rows"] for chunk in parsed["chunks"])


def test_document_upload_api_supports_multiple_files_and_stable_ids(tmp_path) -> None:
    async def run() -> None:
        store = SmartFillDocumentStore(tmp_path / "documents")
        app = FastAPI()
        app.include_router(create_smart_autofill_router(store))
        transport = httpx.ASGITransport(app=app)
        files = [
            ("files", (PDF_FIXTURE.name, PDF_FIXTURE.read_bytes(), "application/pdf")),
            (
                "files",
                (
                    DOCX_FIXTURE.name,
                    DOCX_FIXTURE.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            ),
        ]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/smart-autofill/documents", files=files)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert len(payload["document_ids"]) == 2
            assert len(set(payload["document_ids"])) == 2
            detail = await client.get(
                f"/smart-autofill/documents/{payload['document_ids'][0]}",
                params={"include_chunks": "false"},
            )
            assert detail.status_code == 200
            assert "chunks" not in detail.json()

    asyncio.run(run())


def test_document_ingestion_rejects_unsupported_type(tmp_path) -> None:
    store = SmartFillDocumentStore(tmp_path / "documents")
    with pytest.raises(ValueError, match="Unsupported document type"):
        store.ingest("notes.txt", b"not supported")
