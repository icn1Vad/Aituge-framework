from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iron_report.api import create_router
from iron_report.config import Settings
from iron_report.errors import IronReportError
from iron_report.schemas import IronReportCreateRequest
from iron_report.service import IronReportService, RequestContext
from scheduling.scheduler import SchedulingRuntimeOptions


DATA_ROOT = Path(__file__).resolve().parents[1] / "demo_data"


class FakeTasks:
    def __init__(self) -> None:
        self.task = None

    async def create_task(self, request):
        if self.task is None:
            self.task = SimpleNamespace(
                id="task-1",
                current_run_id=None,
                status="created",
                input_payload_json=request.input_payload,
            )
        return self.task

    async def start_task_run(self, task_id, request):
        self.task.current_run_id = "run-1"
        self.task.status = "running"
        return SimpleNamespace(id="run-1", status="running")


def _service(tmp_path: Path) -> IronReportService:
    settings = Settings(
        data_root=DATA_ROOT,
        runtime_root=tmp_path,
        internal_token="test-internal-token",
        model_id="configured-test-model",
    )
    options = SchedulingRuntimeOptions(
        local_python_artifact_dir=tmp_path / "artifacts",
        local_python_work_dir=tmp_path / "work",
        rag_store=None,
    )
    service = IronReportService(settings, options)
    service.tasks = FakeTasks()
    return service


@pytest.mark.asyncio
async def test_create_contract_is_idempotent_and_keeps_both_dates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    context = RequestContext("7", "11", "13", "request-1")
    request = IronReportCreateRequest.model_validate(
        {
            "reportType": "DAILY",
            "asOfDate": "2026-07-12",
            "outputFormats": ["DOCX", "PDF"],
            "includeWebResearch": True,
        }
    )

    created = await service.create_report(context, request, "same-key")
    reused = await service.create_report(context, request, "same-key")

    assert created.report_date == date(2026, 7, 12)
    assert created.data_as_of_date == date(2026, 7, 10)
    assert created.reused is False
    assert reused.reused is True


@pytest.mark.asyncio
async def test_same_idempotency_key_rejects_different_request(tmp_path: Path) -> None:
    service = _service(tmp_path)
    context = RequestContext("7", "11", "13", "request-1")
    first = IronReportCreateRequest.model_validate(
        {"reportType": "DAILY", "asOfDate": "2026-07-17", "outputFormats": ["DOCX"]}
    )
    second = IronReportCreateRequest.model_validate(
        {"reportType": "WEEKLY", "asOfDate": "2026-07-17", "outputFormats": ["DOCX"]}
    )
    await service.create_report(context, first, "same-key")

    with pytest.raises(IronReportError) as caught:
        await service.create_report(context, second, "same-key")

    assert caught.value.code == "IRON_REPORT_IDEMPOTENCY_CONFLICT"


def test_internal_create_route_declares_http_202(tmp_path: Path) -> None:
    router = create_router(_service(tmp_path))
    route = next(
        route for route in router.routes
        if getattr(route, "path", "") == "/v1/iron-reports" and "POST" in getattr(route, "methods", set())
    )

    assert route.status_code == 202


def test_internal_create_route_actually_returns_http_202(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_router(_service(tmp_path)))

    with TestClient(app) as client:
        response = client.post(
            "/v1/iron-reports",
            headers={
                "Idempotency-Key": "http-contract-key",
                "X-User-Id": "7",
                "X-Tenant-Id": "11",
                "X-Dept-Id": "13",
                "X-Internal-Token": "test-internal-token",
            },
            json={
                "reportType": "DAILY",
                "asOfDate": "2026-07-17",
                "outputFormats": ["DOCX", "PDF"],
                "includeWebResearch": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["success"] is True
