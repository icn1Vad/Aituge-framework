from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

import backend.local_code_chat_app as app_module
from smart_autofill.business_api import _chunk_matches_evidence, _highlight_quote
import smart_autofill.business_api as business_api_module
from db.db_context import init_db, reset_engine_for_test


DOCX = Path(r"E:\MyProjects\方案\03-2关于航天长征化学工程股份有限公司收购航天氢能有限公司股权的可行性研究报告（公开）.docx")


def test_source_preview_matches_anchor_and_highlights_exact_quote() -> None:
    chunk = {
        "text": "项目净现值35309万元，内部收益率9.72%。",
        "anchor": {"paragraph_index": 88},
    }
    evidence = {"paragraph_index": 88, "quote": "内部收益率9.72%"}
    assert _chunk_matches_evidence(chunk, evidence) is True
    assert "<mark>内部收益率9.72%</mark>" in _highlight_quote(chunk["text"], evidence["quote"])


def test_business_task_upload_parse_save_and_reload(tmp_path, monkeypatch) -> None:
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'business.db'}")
        monkeypatch.setenv("SMART_FILL_DOCUMENT_DIR", str(tmp_path / "documents"))
        app_module.SMART_FILL_DOCUMENT_STORE.root = tmp_path / "documents"
        app_module.SMART_FILL_DOCUMENT_STORE.files_dir = tmp_path / "documents" / "files"
        app_module.SMART_FILL_DOCUMENT_STORE.parsed_dir = tmp_path / "documents" / "parsed"
        async def fake_profile(_session, _agent_id):
            return object()

        async def fake_chat(_self, _profile, request):
            assert request.extra_tools == ["rag_retrieval"]
            return {"response": {"choices": [{"message": {"content": "基于知识库的回答"}}]}}

        monkeypatch.setattr(business_api_module, "get_agent_profile", fake_profile)
        monkeypatch.setattr(business_api_module.SchedulingService, "chat", fake_chat)
        reset_engine_for_test()
        await init_db()
        app = app_module.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/api/health")
            assert health.status_code == 200
            created = await client.post("/api/tasks", json={"task_name": "可研测试"})
            assert created.status_code == 200
            task_id = created.json()["task_id"]
            uploaded = await client.post(
                f"/api/tasks/{task_id}/upload",
                files={"file": (DOCX.name, DOCX.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            )
            assert uploaded.status_code == 200
            assert uploaded.json()["parse_status"] == "uploaded"
            knowledge_list = await client.get("/api/knowledge/documents")
            assert knowledge_list.status_code == 200
            assert knowledge_list.json()["items"][0]["chunk_count"] > 150
            chat = await client.post(
                "/api/knowledge/chat",
                json={"question": "航天氢能的主营业务是什么？", "session_id": None},
            )
            assert chat.status_code == 200, chat.text
            assert chat.json()["answer"] == "基于知识库的回答"
            assert chat.json()["sources"]
            parsed = await client.post(f"/api/tasks/{task_id}/parse")
            assert parsed.status_code == 200
            assert parsed.json()["parse_status"] == "success"
            sections = await client.get(f"/api/tasks/{task_id}/sections")
            assert len(sections.json()["items"]) > 150
            saved = await client.put(
                f"/api/tasks/{task_id}/results",
                json={"items": [{"field_id": "project_name", "field_value": "人工项目名称"}]},
            )
            assert saved.status_code == 200
            reloaded = await client.get(f"/api/tasks/{task_id}/results")
            result = reloaded.json()["items"][0]
            assert result["field_value"] == "人工项目名称"
            assert result["is_manual_modified"] is True
            tasks = await client.get("/api/tasks")
            assert tasks.json()["items"][0]["task_id"] == task_id

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
