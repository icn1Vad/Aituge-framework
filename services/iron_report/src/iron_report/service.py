from __future__ import annotations

import asyncio
import hmac
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.artifact_service import resolve_artifact_path
from task_manager.schemas import TaskCreateRequest, TaskRunRequest
from task_manager.service import TaskManagerService

from iron_report.config import Settings
from iron_report.data import IronReportDataRepository
from iron_report.errors import IronReportError
from iron_report.exporter import ExportStateStore, IronReportExporter
from iron_report.schemas import (
    IronReportArtifact,
    IronReportCreateRequest,
    IronReportCreated,
    IronReportStatus,
    IronReportTaskInput,
    ReportType,
)


TASK_TYPE = "iron.report.generate"


@dataclass(frozen=True, slots=True)
class RequestContext:
    user_id: str
    tenant_id: str
    dept_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    path: Path
    name: str
    mime_type: str
    sha256: str


class IronReportService:
    def __init__(self, settings: Settings, options: SchedulingRuntimeOptions) -> None:
        self.settings = settings
        self.options = options
        self.data = IronReportDataRepository(settings.data_root)
        self.tasks = TaskManagerService(options)
        self.export_state = ExportStateStore(settings.export_state_root / "state")
        self.exporter = IronReportExporter(settings, options, self.export_state)
        self._export_locks: dict[str, asyncio.Lock] = {}

    def verify_internal_token(self, token: str) -> None:
        expected = self.settings.internal_token.encode("utf-8")
        actual = (token or "").encode("utf-8")
        if not actual or not hmac.compare_digest(expected, actual):
            raise IronReportError(
                "IRON_REPORT_UNAUTHORIZED",
                "内部调用凭证无效",
                status_code=401,
            )

    async def create_report(
        self,
        context: RequestContext,
        request: IronReportCreateRequest,
        idempotency_key: str,
    ) -> IronReportCreated:
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise IronReportError(
                "IRON_REPORT_IDEMPOTENCY_KEY_INVALID",
                "Idempotency-Key 必须为 1 到 128 个字符",
                status_code=400,
            )
        validated = self.data.validate_report_date(request.report_type, request.as_of_date)
        task_input = IronReportTaskInput(
            report_type=request.report_type,
            report_date=request.as_of_date,
            data_as_of_date=validated.data_as_of_date,
            output_formats=request.output_formats,
            include_web_research=request.include_web_research,
        )
        try:
            task = await self.tasks.create_task(
                TaskCreateRequest(
                    task_type=TASK_TYPE,
                    task_key=f"iron-report:{request.report_type.value}:{request.as_of_date.isoformat()}",
                    idempotency_key=key,
                    title=_title(request.report_type, request.as_of_date),
                    input_payload=task_input.model_dump(mode="json"),
                    user_id=context.user_id,
                    tenant_id=context.tenant_id,
                    stream=False,
                    metadata={
                        "source": "continew-java",
                        "request_id": context.request_id,
                        "dept_id": context.dept_id,
                        "report_date": request.as_of_date.isoformat(),
                        "data_as_of_date": validated.data_as_of_date.isoformat(),
                    },
                )
            )
        except ValueError as exc:
            raise IronReportError(
                "IRON_REPORT_TASK_CREATE_FAILED",
                str(exc),
                status_code=400,
            ) from exc
        expected_input = task_input.model_dump(mode="json")
        if dict(task.input_payload_json or {}) != expected_input:
            raise IronReportError(
                "IRON_REPORT_IDEMPOTENCY_CONFLICT",
                "该幂等键已用于不同的铁矿石报告请求",
                status_code=409,
            )
        reused = task.current_run_id is not None or task.status not in {"created", "pending"}
        try:
            run = await self.tasks.start_task_run(
                task.id,
                TaskRunRequest(
                    stream=False,
                    user_id=context.user_id,
                    idempotency_key=f"{key}:run",
                    metadata_patch={"request_id": context.request_id},
                ),
            )
        except ValueError as exc:
            raise IronReportError(
                "IRON_REPORT_TASK_START_FAILED",
                str(exc),
                status_code=409,
                retryable=True,
            ) from exc
        return IronReportCreated(
            report_id=task.id,
            framework_task_id=task.id,
            framework_run_id=run.id,
            report_type=request.report_type,
            report_date=request.as_of_date,
            data_as_of_date=validated.data_as_of_date,
            status="RUNNING" if task.status in {"pending", "running"} else task.status.upper(),
            reused=reused,
        )

    async def get_status(self, context: RequestContext, report_id: str) -> IronReportStatus:
        task = await self._require_task(context, report_id)
        if task.status == "succeeded" and self.export_state.get(task.id).get("status") != "COMPLETED":
            await self._ensure_export(task)
        task_input = IronReportTaskInput.model_validate(task.input_payload_json)
        artifacts = await self._artifacts(task.id)
        export_state = self.export_state.get(task.id)
        runtime_stage = await self._runtime_stage(task.id) if task.status == "running" else None
        if task.status == "succeeded" and export_state.get("status") != "COMPLETED":
            export_state = {
                "status": "FAILED",
                "error_code": "IRON_REPORT_EXPORT_STATE_MISSING",
                "error_message": "报告任务完成但导出状态不存在",
                "retryable": True,
            }
        status, progress, stage = _status_view(task.status, export_state, runtime_stage)
        error = dict(task.error_payload_json or {})
        if export_state.get("status") == "FAILED":
            error = {
                "type": export_state.get("error_code"),
                "message": export_state.get("error_message"),
                "retryable": export_state.get("retryable"),
            }
        return IronReportStatus(
            report_id=task.id,
            framework_task_id=task.id,
            framework_run_id=task.current_run_id,
            report_type=task_input.report_type,
            report_date=task_input.report_date,
            data_as_of_date=task_input.data_as_of_date,
            status=status,
            progress=progress,
            current_stage=stage,
            error_code=str(error.get("type") or error.get("code") or "") or None,
            error_message=str(error.get("message") or "") or None,
            retryable=bool(error.get("retryable")) if error else None,
            created_at=task.created_at,
            started_at=task.started_at,
            completed_at=task.finished_at,
            artifacts=artifacts,
        )

    async def accept_task_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.exporter.accept_task_result(payload)

    async def get_result(self, context: RequestContext, report_id: str) -> dict[str, Any]:
        task = await self._require_task(context, report_id)
        if task.status == "succeeded" and self.export_state.get(task.id).get("status") != "COMPLETED":
            await self._ensure_export(task)
        if task.status == "failed":
            error = dict(task.error_payload_json or {})
            raise IronReportError(
                str(error.get("type") or "IRON_REPORT_GENERATION_FAILED"),
                str(error.get("message") or "铁矿石报告生成失败"),
                status_code=422,
                retryable=bool(error.get("retryable")),
            )
        export_state = self.export_state.get(task.id)
        if export_state.get("status") == "FAILED":
            raise IronReportError(
                str(export_state.get("error_code") or "IRON_REPORT_EXPORT_FAILED"),
                str(export_state.get("error_message") or "铁矿石报告导出失败"),
                status_code=422,
                retryable=bool(export_state.get("retryable")),
                details=dict(export_state.get("details") or {}),
            )
        if (
            task.status != "succeeded"
            or export_state.get("status") != "COMPLETED"
            or not task.result_payload_json
        ):
            raise IronReportError(
                "IRON_REPORT_NOT_READY",
                "铁矿石报告尚未生成完成",
                status_code=409,
                retryable=True,
            )
        return self._structured_result(task)

    async def list_artifacts(self, context: RequestContext, report_id: str) -> list[IronReportArtifact]:
        task = await self._require_task(context, report_id)
        if task.status == "succeeded" and self.export_state.get(task.id).get("status") != "COMPLETED":
            await self._ensure_export(task)
        return await self._artifacts(task.id)

    async def _ensure_export(self, task) -> None:
        lock = self._export_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            if self.export_state.get(task.id).get("status") == "COMPLETED":
                return
            try:
                await self.exporter.accept_task_result(
                    {
                        "task_id": task.id,
                        "run_id": task.current_run_id,
                        "task_type": task.task_type,
                        "status": "completed",
                        "output": self._structured_result(task),
                    }
                )
            except IronReportError as exc:
                # The exporter persists its stable failure state. Status reads expose that state
                # so one export failure does not become an unstructured HTTP 500.
                if self.export_state.get(task.id).get("status") != "FAILED":
                    self.export_state.save(
                        task.id,
                        {
                            "status": "FAILED",
                            "stage": "FAILED",
                            "error_code": exc.code,
                            "error_message": str(exc),
                            "retryable": exc.retryable,
                            "details": exc.details,
                        },
                    )
                return

    def _structured_result(self, task) -> dict[str, Any]:
        result = dict(task.result_payload_json or {})
        structured = result.get("structured")
        if not isinstance(structured, dict):
            raise IronReportError(
                "IRON_REPORT_STRUCTURED_RESULT_MISSING",
                "Framework 未保存可导出的结构化报告结果",
                status_code=422,
                retryable=True,
            )
        return dict(structured)

    async def get_artifact_content(
        self,
        context: RequestContext,
        report_id: str,
        artifact_id: str,
    ) -> ArtifactContent:
        await self._require_task(context, report_id)
        row = await self.tasks.get_artifact(artifact_id)
        if row is None or row.task_id != report_id:
            raise IronReportError(
                "IRON_REPORT_ARTIFACT_NOT_FOUND",
                "报告产物不存在",
                status_code=404,
            )
        if not row.content_uri:
            raise IronReportError(
                "IRON_REPORT_ARTIFACT_NOT_READY",
                "报告产物内容尚未就绪",
                status_code=409,
                retryable=True,
            )
        try:
            path = resolve_artifact_path(self.options.local_python_artifact_dir, row.content_uri)
        except ValueError as exc:
            raise IronReportError(
                "IRON_REPORT_ARTIFACT_PATH_INVALID",
                "报告产物路径无效",
                status_code=500,
            ) from exc
        if not path.is_file():
            raise IronReportError(
                "IRON_REPORT_ARTIFACT_NOT_FOUND",
                "报告产物内容不存在",
                status_code=404,
            )
        actual_checksum = _sha256(path)
        if actual_checksum != row.checksum:
            raise IronReportError(
                "IRON_REPORT_ARTIFACT_CORRUPT",
                "报告产物 SHA-256 校验失败",
                status_code=409,
                details={"expected": row.checksum, "actual": actual_checksum},
            )
        if path.stat().st_size > self.settings.max_artifact_bytes:
            raise IronReportError(
                "IRON_REPORT_ARTIFACT_TOO_LARGE",
                "报告产物超过允许下载大小",
                status_code=413,
            )
        metadata = dict(row.metadata_json or {})
        return ArtifactContent(
            path=path,
            name=str(metadata.get("name") or path.name),
            mime_type=str(metadata.get("mime") or "application/octet-stream"),
            sha256=row.checksum,
        )

    async def _require_task(self, context: RequestContext, report_id: str):
        task = await self.tasks.get_task(report_id)
        if (
            task is None
            or task.task_type != TASK_TYPE
            or task.user_id != context.user_id
            or task.tenant_id != context.tenant_id
        ):
            raise IronReportError(
                "IRON_REPORT_NOT_FOUND",
                "铁矿石报告任务不存在",
                status_code=404,
            )
        return task

    async def _runtime_stage(self, task_id: str) -> str:
        events = await self.tasks.list_events(task_id, limit=200, offset=0)
        stage = "ANALYZING"
        for event in events:
            if event.event_type == "agent_final":
                stage = "WRITING_REPORT"
                continue
            if event.event_type not in {"tool_started", "tool_completed"}:
                continue
            tool_name = str((event.payload_json or {}).get("tool_name") or "").lower()
            if "iron_market_data" in tool_name:
                stage = "LOADING_DATA"
            elif "search" in tool_name:
                stage = "RESEARCHING"
            elif "python" in tool_name or "code" in tool_name:
                stage = "GENERATING_CHARTS"
        return stage

    async def _artifacts(self, task_id: str) -> list[IronReportArtifact]:
        rows = await self.tasks.list_task_artifacts(task_id)
        result: list[IronReportArtifact] = []
        for row in rows:
            if not row.content_uri:
                continue
            metadata = dict(row.metadata_json or {})
            mime = str(metadata.get("mime") or "application/octet-stream")
            name = str(metadata.get("name") or row.summary or row.id)
            result.append(
                IronReportArtifact(
                    artifact_id=row.id,
                    kind=_artifact_kind(mime, name),
                    name=name,
                    mime_type=mime,
                    size=_artifact_size(self.options.local_python_artifact_dir, row.content_uri),
                    sha256=row.checksum,
                    content_url=f"/v1/iron-reports/{task_id}/artifacts/{row.id}/content",
                )
            )
        return result


def _status_view(
    status: str,
    export_state: dict[str, Any],
    runtime_stage: str | None = None,
) -> tuple[str, int, str]:
    if export_state.get("status") == "FAILED":
        return "FAILED", 100, "FAILED"
    if export_state.get("stage") == "EXPORTING":
        return "RUNNING", int(export_state.get("progress") or 90), "EXPORTING"
    if status == "pending":
        return "CREATED", 0, "CREATED"
    if status == "running":
        progress_by_stage = {
            "LOADING_DATA": 15,
            "RESEARCHING": 30,
            "ANALYZING": 45,
            "GENERATING_CHARTS": 65,
            "WRITING_REPORT": 80,
        }
        stage = runtime_stage or "ANALYZING"
        return "RUNNING", progress_by_stage.get(stage, 45), stage
    if status == "succeeded":
        return "SUCCEEDED", 100, "COMPLETED"
    if status == "cancelled":
        return "FAILED", 100, "FAILED"
    return "FAILED", 100, "FAILED"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_kind(mime: str, name: str) -> str:
    value = mime.lower()
    suffix = Path(name).suffix.lower()
    if value == "application/pdf" or suffix == ".pdf":
        return "PDF"
    if value == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or suffix == ".docx":
        return "DOCX"
    if value.startswith("image/"):
        return "CHART"
    return "OTHER"


def _artifact_size(root: Path, content_uri: str) -> int | None:
    try:
        path = resolve_artifact_path(root, content_uri)
    except ValueError:
        return None
    return path.stat().st_size if path.is_file() else None


def _title(report_type: ReportType, report_date) -> str:
    label = "日报" if report_type == ReportType.DAILY else "周报"
    return f"铁矿石市场{label}（{report_date.isoformat()}）"
