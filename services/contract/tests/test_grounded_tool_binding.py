from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from services.contract.capabilities import register as contract_capability
from services.contract.capabilities.grounded_answer import GroundedAnswerTaskInput


class _Response:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def test_review_result_tool_uses_task_ids_when_model_supplies_wrong_ids(monkeypatch) -> None:
    expected = GroundedAnswerTaskInput(
        schema_version="1.0",
        mode="CHAT",
        review_id="review-correct",
        document_id="document-correct",
        question="这份合同有什么风险？",
    )
    captured: dict = {}

    async def load_bound_input(*, task_id: str, tenant_id: str):
        captured["task_id"] = task_id
        captured["tenant_id"] = tenant_id
        return expected

    class _Client:
        def __init__(self, *, base_url: str, timeout: int) -> None:
            captured["base_url"] = base_url
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, *, headers: dict, json: dict) -> _Response:
            captured["path"] = path
            captured["headers"] = headers
            captured["json"] = json
            return _Response({"success": True, "data": {"result": {"findings": []}}})

    monkeypatch.setattr(contract_capability, "_load_bound_grounded_answer_input", load_bound_input)
    monkeypatch.setattr(contract_capability.httpx, "AsyncClient", _Client)

    config = SimpleNamespace(
        tenant_id="tenant-test",
        config={
            "artifact_publisher": SimpleNamespace(
                task_id="task-current",
                stage_run_id="stage-current",
            )
        },
    )
    bundle = contract_capability._grounded_review_result_tool_factory(
        "http://contract:18200",
        "internal-token",
    )(config)

    output = asyncio.run(
        bundle.tools[0].acall(
            review_id="review-wrong",
            document_id="document-wrong",
        )
    )

    assert json.loads(output.content)["success"] is True
    assert captured["task_id"] == "task-current"
    assert captured["tenant_id"] == "tenant-test"
    assert captured["json"] == {
        "review_id": "review-correct",
        "document_id": "document-correct",
    }
