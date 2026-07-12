from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

import backend.local_code_chat_app as app_module
from db.db_context import init_db, reset_engine_for_test


DOCX = Path(r"E:\MyProjects\方案\03-2关于航天长征化学工程股份有限公司收购航天氢能有限公司股权的可行性研究报告（公开）.docx")


def test_business_task_upload_parse_save_and_reload(tmp_path, monkeypatch) -> None:
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'business.db'}")
        monkeypatch.setenv("SMART_FILL_DOCUMENT_DIR", str(tmp_path / "documents"))
        app_module.SMART_FILL_DOCUMENT_STORE.root = tmp_path / "documents"
        app_module.SMART_FILL_DOCUMENT_STORE.files_dir = tmp_path / "documents" / "files"
        app_module.SMART_FILL_DOCUMENT_STORE.parsed_dir = tmp_path / "documents" / "parsed"
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
