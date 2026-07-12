from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import delete, select

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.schemas import TaskCreateRequest, TaskRunRequest
from task_manager.service import TaskManagerService

from .business_models import SmartFillAuditRecord, SmartFillEvidenceRecord, SmartFillFieldValue, SmartFillTask, now
from .documents import SmartFillDocumentStore
from .extraction import CONTRACT_DIR, build_extraction_input


class TaskCreate(BaseModel):
    task_name: str


class ResultsUpdate(BaseModel):
    items: list[dict[str, Any]]


def create_smart_fill_business_router(options: SchedulingRuntimeOptions, store: SmartFillDocumentStore) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["smart-autofill-business"])

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": "Aituge SmartAutoFill"}

    @router.get("/form-config")
    async def form_config():
        return json.loads((CONTRACT_DIR / "smart_fill_field_schema.json").read_text(encoding="utf-8"))

    @router.get("/field-rules")
    async def field_rules():
        return {"rules": []}

    @router.get("/tasks")
    async def list_tasks():
        async with create_db_session() as session:
            rows = (await session.exec(select(SmartFillTask).order_by(SmartFillTask.created_at.desc()))).all()
        return {"items": [_task(row) for row in rows]}

    @router.post("/tasks")
    async def create_task(payload: TaskCreate):
        row = SmartFillTask(task_name=payload.task_name.strip() or "未命名任务")
        async with create_db_session() as session:
            session.add(row)
        return _task(row)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        return _task(await _get_task(task_id))

    @router.post("/tasks/{task_id}/upload")
    async def upload(task_id: str, file: UploadFile = File(...)):
        row = await _get_task(task_id)
        content = await file.read()
        try:
            doc = store.ingest(file.filename or "upload", content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        row.document_id = doc["document_id"]
        row.report_file_name = doc["file_name"]
        row.report_file_size = doc["size_bytes"]
        row.parse_status = "uploaded"
        row.section_count = doc["paragraph_count"] or doc["page_count"] or 0
        row.table_count = doc["table_count"]
        row.updated_at = now()
        async with create_db_session() as session:
            session.add(row)
        return _task(row)

    @router.post("/tasks/{task_id}/parse")
    async def parse(task_id: str):
        row = await _get_task(task_id)
        if not row.document_id:
            raise HTTPException(400, "请先上传PDF或DOCX文件")
        parsed = store.get(row.document_id)
        row.parse_status = "success"
        row.section_count = len(parsed["chunks"])
        row.table_count = parsed["table_count"]
        row.updated_at = now()
        async with create_db_session() as session:
            session.add(row)
        return _task(row)

    @router.get("/tasks/{task_id}/sections")
    async def sections(task_id: str):
        row = await _get_task(task_id)
        parsed = store.get(row.document_id or "")
        return {"items": [{
            "section_id": chunk["chunk_id"], "title": chunk["anchor"].get("section") or f"{chunk['kind']} {chunk['index'] + 1}",
            "level": 1, "content_type": chunk["kind"], "content": chunk["text"],
            "paragraph_index": chunk["anchor"].get("paragraph_index"), "keywords": [],
        } for chunk in parsed["chunks"]]}

    @router.post("/tasks/{task_id}/fill")
    async def fill(task_id: str):
        row = await _get_task(task_id)
        if row.parse_status != "success" or not row.document_id:
            raise HTTPException(400, "请先完成文档解析")
        payload = build_extraction_input([row.document_id])
        manager = TaskManagerService(options)
        tm_task = await manager.create_task(TaskCreateRequest(
            task_type="form.smart_fill.extract", title=row.task_name, input_payload=payload,
            user_id="default_user", stream=False,
        ))
        async for _ in manager.stream_task(tm_task.id, TaskRunRequest(stream=False, user_id="default_user")):
            pass
        items = await manager.list_items(tm_task.id)
        failed_items = [item for item in items if item.status != "succeeded"]
        row.task_manager_id = tm_task.id
        if failed_items:
            row.fill_status = "failed"
            row.updated_at = now()
            async with create_db_session() as session:
                session.add(row)
            failed_groups = ", ".join(item.item_key for item in failed_items)
            raise HTTPException(502, f"自动填单失败：{failed_groups}")
        await _persist_agent_results(row.id, items)
        row.fill_status = "success"
        row.updated_at = now()
        async with create_db_session() as session:
            session.add(row)
        return {"items": await _results(row.id)}

    @router.get("/tasks/{task_id}/results")
    async def results(task_id: str):
        await _get_task(task_id)
        return {"items": await _results(task_id)}

    @router.put("/tasks/{task_id}/results")
    async def update_results(task_id: str, payload: ResultsUpdate):
        await _get_task(task_id)
        async with create_db_session() as session:
            for update in payload.items:
                field_id = update["field_id"]
                current = (await session.exec(select(SmartFillFieldValue).where(
                    SmartFillFieldValue.task_id == task_id, SmartFillFieldValue.field_id == field_id))).first()
                new_value = _decode_value(update.get("field_value"))
                if current is None:
                    current = SmartFillFieldValue(task_id=task_id, field_id=field_id)
                session.add(SmartFillAuditRecord(task_id=task_id, field_id=field_id, action="manual_update",
                                                 old_value_json=current.value_json, new_value_json=new_value))
                current.value_json = new_value
                current.status = "filled" if new_value not in (None, "", []) else "missing"
                current.is_manual_modified = True
                current.updated_at = now()
                session.add(current)
        return {"items": await _results(task_id)}

    @router.post("/tasks/{task_id}/validate")
    async def validate(task_id: str):
        values = await _results(task_id)
        return {"items": [{"field_id": item["field_id"], "field_name": item["field_name"],
                           "field_group": "", "exception_type": "missing", "message": "字段缺失",
                           "suggestion": "请人工检查", "current_value": ""}
                          for item in values if not item["field_value"]]}

    @router.get("/tasks/{task_id}/word-preview", response_class=HTMLResponse)
    async def preview(task_id: str, result_id: str = ""):
        row = await _get_task(task_id)
        parsed = store.get(row.document_id or "")
        body = "".join(f"<p id='{c['chunk_id']}'>{_escape(c['text'])}</p>" for c in parsed["chunks"])
        return HTMLResponse(f"<html><meta charset='utf-8'><body>{body}</body></html>")

    return router


async def _get_task(task_id: str) -> SmartFillTask:
    async with create_db_session() as session:
        row = await session.get(SmartFillTask, task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    return row


def _task(row: SmartFillTask):
    return {"task_id": row.id, "task_name": row.task_name, "report_file_name": row.report_file_name,
            "report_file_size": row.report_file_size, "uploaded_at": row.updated_at, "parsed_at": row.updated_at,
            "section_count": row.section_count, "table_count": row.table_count, "parse_error": None,
            "parse_status": row.parse_status, "fill_status": row.fill_status, "review_status": row.review_status,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "progress_task_id": row.task_manager_id}


async def _persist_agent_results(task_id: str, items) -> None:
    schema = json.loads((CONTRACT_DIR / "smart_fill_field_schema.json").read_text(encoding="utf-8"))
    names = {f["field_id"]: f["field_name"] for g in schema["groups"] for f in g["fields"]}
    async with create_db_session() as session:
        for item in items:
            result = ((item.result_payload_json or {}).get("result") or {})
            for field in result.get("fields") or []:
                field_id = field["field_id"]
                current = (await session.exec(select(SmartFillFieldValue).where(
                    SmartFillFieldValue.task_id == task_id, SmartFillFieldValue.field_id == field_id))).first()
                if current is not None and current.is_manual_modified:
                    continue
                if current is None:
                    current = SmartFillFieldValue(task_id=task_id, field_id=field_id)
                current.field_name = names.get(field_id, field_id)
                current.value_json = field.get("value")
                current.status = field.get("status", "missing")
                current.updated_at = now()
                session.add(current)
                await session.exec(delete(SmartFillEvidenceRecord).where(
                    SmartFillEvidenceRecord.task_id == task_id, SmartFillEvidenceRecord.field_id == field_id))
                for evidence in field.get("evidence") or []:
                    session.add(SmartFillEvidenceRecord(task_id=task_id, field_id=field_id, evidence_json=evidence))


async def _results(task_id: str):
    async with create_db_session() as session:
        values = (await session.exec(select(SmartFillFieldValue).where(SmartFillFieldValue.task_id == task_id))).all()
        evidence = (await session.exec(select(SmartFillEvidenceRecord).where(SmartFillEvidenceRecord.task_id == task_id))).all()
    by_field: dict[str, list[dict]] = {}
    for row in evidence:
        by_field.setdefault(row.field_id, []).append(row.evidence_json)
    result = []
    for row in values:
        ev = (by_field.get(row.field_id) or [{}])[0]
        value = json.dumps(row.value_json, ensure_ascii=False) if isinstance(row.value_json, (list, dict)) else str(row.value_json or "")
        result.append({"result_id": row.id, "task_id": task_id, "field_id": row.field_id,
                       "field_name": row.field_name, "field_value": value,
                       "source_section": ev.get("section"), "source_text": ev.get("quote"),
                       "exact_quote": ev.get("quote"), "source_section_id": None,
                       "source_paragraph_index": ev.get("paragraph_index"), "char_start": ev.get("char_start"),
                       "char_end": ev.get("char_end"), "confidence_score": 0, "confidence_reasons": "[]",
                       "confidence": 0, "generate_type": "manual" if row.is_manual_modified else "agent",
                       "is_manual_modified": row.is_manual_modified, "is_confirmed": row.is_confirmed,
                       "exception_message": None, "status": row.status, "updated_at": row.updated_at})
    return result


def _decode_value(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
