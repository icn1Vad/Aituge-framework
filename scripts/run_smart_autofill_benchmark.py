from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def parse_args():
    parser = argparse.ArgumentParser(description="Run a real-model SmartAutoFill extraction benchmark.")
    parser.add_argument("document", type=Path)
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path, required=True)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    model_config = _read_model_config(ROOT / "backend" / "single-agent" / "tmp" / "sqlite" / "local.db")
    os.environ["SMART_FILL_DOCUMENT_DIR"] = str(args.storage_dir.resolve())
    os.environ["SQLITE_URL"] = f"sqlite+aiosqlite:///{args.sqlite.resolve()}"

    from backend.local_code_chat_app import create_app
    from common.system_constants import DEFAULT_TENANT_ID
    from db.db_context import create_db_session, init_db
    from db.models.llm import LlmModelEntity
    from scheduling.agent_registry import ensure_default_agent_profiles
    from skill import ensure_default_skill_packages

    await init_db()
    async with create_db_session() as session:
        session.add(LlmModelEntity(**model_config))
    async with create_db_session() as session:
        await ensure_default_skill_packages(session)
        await ensure_default_agent_profiles(session)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    headers = {"X-User-Id": "default_user", "X-Tenant-Id": DEFAULT_TENANT_ID}
    started = time.perf_counter()
    async with httpx.AsyncClient(transport=transport, base_url="http://benchmark", timeout=None) as client:
        with args.document.open("rb") as source:
            upload = await client.post(
                "/smart-autofill/documents",
                files={"files": (args.document.name, source, _media_type(args.document))},
            )
        upload.raise_for_status()
        document_id = upload.json()["document_ids"][0]
        extraction_input = await client.post(
            "/smart-autofill/extraction-input",
            json=[document_id],
        )
        extraction_input.raise_for_status()
        run = await client.post(
            "/task-manager/run",
            headers=headers,
            json={
                "task_type": "form.smart_fill.extract",
                "title": f"Real model benchmark: {args.document.name}",
                "input_payload": extraction_input.json(),
                "stream": False,
            },
        )
        elapsed = time.perf_counter() - started
        run.raise_for_status()
        task = run.json()["task"]
        items_response = await client.get(f"/task-manager/tasks/{task['id']}/items", headers=headers)
        items_response.raise_for_status()
        items = items_response.json()["items"]
        events_response = await client.get(f"/task-manager/tasks/{task['id']}/events", headers=headers)
        events_response.raise_for_status()
        events = events_response.json()["events"]

    report = {
        "benchmark_id": f"smart-fill-real-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_id": "deepseek-v4-pro",
        "document": {"path": str(args.document.resolve()), "document_id": document_id},
        "elapsed_seconds": round(elapsed, 3),
        "task_id": task["id"],
        "task_status": task["status"],
        "metrics": _metrics(items, events),
        "items": items,
        "validation_events": [
            event for event in events
            if event["event_type"] in {"item_output_parse_failed", "item_output_validation_failed"}
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("benchmark_id", "elapsed_seconds", "task_status", "metrics")}, ensure_ascii=False, indent=2))


def _metrics(items: list[dict], events: list[dict]) -> dict:
    fields = []
    for item in items:
        structured = ((item.get("result_payload_json") or {}).get("result") or {})
        fields.extend(structured.get("fields") or [])
    filled = [field for field in fields if field.get("status") == "filled"]
    evidenced = [field for field in filled if field.get("evidence")]
    return {
        "item_total": len(items),
        "item_succeeded": sum(item.get("status") == "succeeded" for item in items),
        "item_failed": sum(item.get("status") == "failed" for item in items),
        "field_results_returned": len(fields),
        "field_filled": len(filled),
        "field_missing_or_review": len(fields) - len(filled),
        "filled_with_evidence": len(evidenced),
        "evidence_coverage_of_filled": round(len(evidenced) / len(filled), 4) if filled else 0,
        "expected_field_count": 79,
        "output_validation_failures": sum(
            event["event_type"] in {"item_output_parse_failed", "item_output_validation_failed"}
            for event in events
        ),
    }


def _media_type(path: Path) -> str:
    return "application/pdf" if path.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _read_model_config(database: Path) -> dict:
    fields = [
        "tenant_id", "base_url", "model", "model_name", "context_window", "temperature",
        "model_id", "enabled", "vision_support", "max_tokens", "enable_thinking",
        "provider_name", "source", "encrypted_api_key",
    ]
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            f"SELECT {', '.join(fields)} FROM tuge_llm_model WHERE model_id = ? AND enabled = 1",
            ("deepseek-v4-pro",),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("Enabled model 'deepseek-v4-pro' was not found in the existing local database.")
    return dict(row)


if __name__ == "__main__":
    asyncio.run(main())
