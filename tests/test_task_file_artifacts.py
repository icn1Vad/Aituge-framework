import asyncio
import hashlib
from pathlib import Path

import httpx
from fastapi import FastAPI

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.api import create_task_manager_router
from task_manager.artifact_service import TaskArtifactPublisher, resolve_artifact_path
from task_manager.models import TaskArtifactEntity, TaskEntity, TaskRunEntity
from task_manager.pipeline.store import create_stage_run, get_artifact


def test_task_artifact_publisher_and_authenticated_content(tmp_path: Path):
    async def run():
        await init_db()
        task_id = "file-artifact-task"
        run_id = "file-artifact-run"
        async with create_db_session() as session:
            session.add(
                TaskEntity(
                    id=task_id,
                    task_type="proof.qa.chat",
                    current_run_id=run_id,
                    user_id="artifact-user",
                    tenant_id=DEFAULT_TENANT_ID,
                )
            )
            session.add(TaskRunEntity(id=run_id, task_id=task_id))
        stage = await create_stage_run(
            task_id=task_id,
            run_id=run_id,
            stage_id="agent",
            stage_type="agent",
            attempt=1,
            agent_id="proof-qa-agent",
            input_artifact_ids=[],
        )

        source = tmp_path / "source.png"
        content = b"small-png-test"
        source.write_bytes(content)
        root = tmp_path / "artifacts"
        ref = await TaskArtifactPublisher(
            root=root,
            task_id=task_id,
            run_id=run_id,
            stage_run_id=stage.id,
        ).publish(source, sequence=1, mime="image/png")
        html_source = tmp_path / "report.html"
        html_source.write_text("<h1>artifact report</h1>", encoding="utf-8")
        html_ref = await TaskArtifactPublisher(
            root=root,
            task_id=task_id,
            run_id=run_id,
            stage_run_id=stage.id,
        ).publish(html_source, sequence=2, mime="text/html")

        assert ref.to_dict() == {
            "id": ref.id,
            "name": "image-001.png",
            "mime": "image/png",
            "url": f"/task-manager/artifacts/{ref.id}/content",
        }
        row = await get_artifact(ref.id)
        assert row is not None
        assert row.task_id == task_id
        assert row.stage_run_id == stage.id
        assert row.checksum == hashlib.sha256(content).hexdigest()
        assert resolve_artifact_path(root, row.content_uri or "").read_bytes() == content

        app = FastAPI()
        app.include_router(
            create_task_manager_router(
                SchedulingRuntimeOptions(
                    local_python_artifact_dir=root,
                    local_python_work_dir=tmp_path / "code-runs",
                )
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                ref.url,
                headers={
                    "X-User-Id": "artifact-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert response.status_code == 200
            assert response.content == content
            assert response.headers["content-type"] == "image/png"
            assert response.headers["content-length"] == str(len(content))
            assert "inline" in response.headers["content-disposition"]
            assert response.headers["x-content-type-options"] == "nosniff"

            html_response = await client.get(
                html_ref.url,
                headers={
                    "X-User-Id": "artifact-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert html_response.status_code == 200
            assert html_response.headers["content-type"].startswith("text/html")
            assert html_response.text == "<h1>artifact report</h1>"

            forbidden = await client.get(
                ref.url,
                headers={
                    "X-User-Id": "other-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert forbidden.status_code == 403

            wrong_tenant = await client.get(
                ref.url,
                headers={
                    "X-User-Id": "artifact-user",
                    "X-Tenant-Id": "other-tenant",
                },
            )
            assert wrong_tenant.status_code == 403

            missing = await client.get(
                "/task-manager/artifacts/does-not-exist/content",
                headers={
                    "X-User-Id": "artifact-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert missing.status_code == 404

            html_row = await get_artifact(html_ref.id)
            assert html_row is not None
            resolve_artifact_path(root, html_row.content_uri or "").unlink()
            missing_file = await client.get(
                html_ref.url,
                headers={
                    "X-User-Id": "artifact-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert missing_file.status_code == 404

        async with create_db_session() as session:
            stored = await session.get(TaskArtifactEntity, ref.id)
            assert stored is not None
            stored.content_uri = "../../outside.png"
            session.add(stored)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            escaped = await client.get(
                ref.url,
                headers={
                    "X-User-Id": "artifact-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert escaped.status_code == 404

    asyncio.run(run())
