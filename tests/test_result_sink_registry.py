import asyncio
from types import SimpleNamespace

import pytest

from task_manager.pipeline.stage_registry import register_stage_handler
from task_manager.result_sink import (
    RequiredResultSinkError,
    ResultSinkRejectedError,
    deliver_task_result,
    is_required_result_sink,
    register_result_sink_handler,
)


def test_capability_result_sink_receives_typed_delivery() -> None:
    deliveries = []

    async def handler(delivery):
        deliveries.append(delivery)

    register_result_sink_handler(
        "test.contract.sink",
        handler,
        source="test-contract",
        required=True,
    )
    assert is_required_result_sink("test.contract.sink") is True
    assert is_required_result_sink("test.contract.missing") is False
    task = SimpleNamespace(
        id="task-1",
        current_run_id="run-1",
        task_type="test.contract.sink",
        input_payload_json={"review_id": "review-1"},
    )
    definition = SimpleNamespace(result_sink_url=None)

    asyncio.run(
        deliver_task_result(
            task,
            definition,
            {"result_type": "STAGE_V1"},
            stage_id="stage-1",
            status="failed",
            error_message="stage failed",
            error_code="RESULT_INVALID",
            retryable=True,
        )
    )

    assert len(deliveries) == 1
    assert deliveries[0].task is task
    assert deliveries[0].stage_id == "stage-1"
    assert deliveries[0].status == "failed"
    assert deliveries[0].output == {"result_type": "STAGE_V1"}
    assert deliveries[0].error_message == "stage failed"
    assert deliveries[0].error_code == "RESULT_INVALID"
    assert deliveries[0].retryable is True


def test_required_capability_result_sink_failure_is_not_downgraded() -> None:
    async def handler(_delivery):
        raise RuntimeError("sink unavailable")

    register_result_sink_handler(
        "test.contract.required-sink",
        handler,
        source="test-contract-required",
        required=True,
    )
    task = SimpleNamespace(
        id="task-2",
        current_run_id="run-2",
        task_type="test.contract.required-sink",
        input_payload_json={},
    )

    with pytest.raises(RequiredResultSinkError, match="sink unavailable"):
        asyncio.run(deliver_task_result(task, SimpleNamespace(result_sink_url=None), None))


def test_required_sink_preserves_a_durable_rejection_as_its_cause() -> None:
    async def handler(_delivery):
        raise ResultSinkRejectedError("result rejected")

    register_result_sink_handler(
        "test.contract.rejected-sink",
        handler,
        source="test-contract-rejected",
        required=True,
    )
    task = SimpleNamespace(
        id="task-rejected",
        current_run_id="run-rejected",
        task_type="test.contract.rejected-sink",
        input_payload_json={},
    )

    with pytest.raises(RequiredResultSinkError) as caught:
        asyncio.run(deliver_task_result(task, SimpleNamespace(result_sink_url=None), None))
    assert isinstance(caught.value.__cause__, ResultSinkRejectedError)


def test_result_sink_registration_rejects_foreign_owner() -> None:
    async def first(_delivery):
        return None

    async def second(_delivery):
        return None

    register_result_sink_handler("test.contract.owned", first, source="owner-a")
    with pytest.raises(ValueError, match="owner-a"):
        register_result_sink_handler("test.contract.owned", second, source="owner-b")


def test_stage_handler_registration_rejects_foreign_owner_even_for_same_callable() -> None:
    async def handler(_context):
        return None

    register_stage_handler("test.contract.owned-stage", handler, source="owner-a")
    with pytest.raises(ValueError, match="owner-a"):
        register_stage_handler("test.contract.owned-stage", handler, source="owner-b")
