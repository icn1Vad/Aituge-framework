import asyncio
from types import SimpleNamespace

from task_manager import result_sink


def test_registered_batch_result_sink_receives_audit_identity(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 15

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            calls.append((url, json))
            return Response()

    monkeypatch.setattr(result_sink.httpx, "AsyncClient", Client)
    task = SimpleNamespace(
        id="task-1",
        current_run_id="run-1",
        task_type="proof.audit.run",
        input_payload_json={"audit_id": "audit-1"},
    )
    definition = SimpleNamespace(result_sink_url="http://proof/v1/internal/semantic-audits/result")
    output = {"summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}, "items": []}

    asyncio.run(result_sink.deliver_task_result(task, definition, output))

    assert calls == [
        (
            "http://proof/v1/internal/semantic-audits/result",
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "task_type": "proof.audit.run",
                "audit_id": "audit-1",
                "status": "completed",
                "output": output,
            },
        )
    ]
