from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import select

from db.db_context import create_db_session
from task_manager.models import (
    TaskArtifactEntity,
    TaskRunEntity,
    TaskStageRunEntity,
    utc_now,
)


async def get_run(run_id: str) -> TaskRunEntity | None:
    async with create_db_session() as session:
        return await session.get(TaskRunEntity, run_id)


async def list_task_runs(task_id: str) -> list[TaskRunEntity]:
    async with create_db_session() as session:
        result = await session.exec(
            select(TaskRunEntity)
            .where(TaskRunEntity.task_id == task_id)
            .order_by(TaskRunEntity.created_at)
        )
        return list(result.all())


async def update_run(run_id: str, **values: Any) -> TaskRunEntity:
    async with create_db_session() as session:
        run = await session.get(TaskRunEntity, run_id)
        if run is None:
            raise ValueError(f"Run '{run_id}' not found.")
        for key, value in values.items():
            if not hasattr(run, key):
                raise ValueError(f"Unknown run field '{key}'.")
            setattr(run, key, value)
        run.updated_at = utc_now()
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def create_stage_run(
    *,
    task_id: str,
    run_id: str,
    stage_id: str,
    stage_type: str,
    attempt: int,
    agent_id: str | None,
    input_artifact_ids: list[str],
) -> TaskStageRunEntity:
    now = utc_now()
    row = TaskStageRunEntity(
        task_id=task_id,
        run_id=run_id,
        stage_id=stage_id,
        stage_type=stage_type,
        attempt=attempt,
        status="running",
        agent_id=agent_id,
        input_artifact_ids_json=input_artifact_ids,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    async with create_db_session() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_stage_run(stage_run_id: str, **values: Any) -> TaskStageRunEntity:
    async with create_db_session() as session:
        row = await session.get(TaskStageRunEntity, stage_run_id)
        if row is None:
            raise ValueError(f"StageRun '{stage_run_id}' not found.")
        for key, value in values.items():
            if not hasattr(row, key):
                raise ValueError(f"Unknown stage run field '{key}'.")
            setattr(row, key, value)
        row.updated_at = utc_now()
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def list_stage_runs(run_id: str) -> list[TaskStageRunEntity]:
    async with create_db_session() as session:
        result = await session.exec(
            select(TaskStageRunEntity)
            .where(TaskStageRunEntity.run_id == run_id)
            .order_by(TaskStageRunEntity.created_at)
        )
        return list(result.all())


async def create_artifact(
    *,
    task_id: str,
    run_id: str,
    stage_run_id: str,
    artifact_type: str,
    schema_name: str,
    content: dict[str, Any],
    parent_artifact_ids: list[str],
    summary: str = "",
    metadata: dict[str, Any] | None = None,
) -> TaskArtifactEntity:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    async with create_db_session() as session:
        version_result = await session.exec(
            select(TaskArtifactEntity)
            .where(TaskArtifactEntity.run_id == run_id)
            .where(TaskArtifactEntity.artifact_type == artifact_type)
            .order_by(TaskArtifactEntity.artifact_version.desc())
        )
        previous = version_result.first()
        artifact = TaskArtifactEntity(
            task_id=task_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            artifact_type=artifact_type,
            artifact_version=(previous.artifact_version + 1) if previous else 1,
            schema_name=schema_name or "",
            content_json=content,
            summary=summary,
            parent_artifact_ids_json=parent_artifact_ids,
            checksum=checksum,
            metadata_json=metadata or {},
        )
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)
        return artifact


async def create_file_artifact(
    *,
    artifact_id: str,
    task_id: str,
    run_id: str,
    stage_run_id: str,
    content_uri: str,
    checksum: str,
    summary: str,
    metadata: dict[str, Any],
) -> TaskArtifactEntity:
    """Persist one task-owned file using the existing Artifact model."""

    artifact_type = "tool_file"
    async with create_db_session() as session:
        version_result = await session.exec(
            select(TaskArtifactEntity)
            .where(TaskArtifactEntity.run_id == run_id)
            .where(TaskArtifactEntity.artifact_type == artifact_type)
            .order_by(TaskArtifactEntity.artifact_version.desc())
        )
        previous = version_result.first()
        artifact = TaskArtifactEntity(
            id=artifact_id,
            task_id=task_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            artifact_type=artifact_type,
            artifact_version=(previous.artifact_version + 1) if previous else 1,
            content_uri=content_uri,
            summary=summary,
            checksum=checksum,
            metadata_json=metadata,
        )
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)
        return artifact


async def get_artifact(artifact_id: str) -> TaskArtifactEntity | None:
    async with create_db_session() as session:
        return await session.get(TaskArtifactEntity, artifact_id)


async def list_artifacts(*, task_id: str | None = None, run_id: str | None = None) -> list[TaskArtifactEntity]:
    if not task_id and not run_id:
        raise ValueError("task_id or run_id is required.")
    async with create_db_session() as session:
        statement = select(TaskArtifactEntity)
        if task_id:
            statement = statement.where(TaskArtifactEntity.task_id == task_id)
        if run_id:
            statement = statement.where(TaskArtifactEntity.run_id == run_id)
        result = await session.exec(statement.order_by(TaskArtifactEntity.created_at))
        return list(result.all())
