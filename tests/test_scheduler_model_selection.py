from task_manager.handlers.scheduler_task import (
    _build_scheduler_input,
    _selected_model_id,
)
from task_manager.models import TaskEntity
from task_manager.registry import TaskType


PROOF_QA = TaskType(
    task_type="proof.qa.chat",
    name="Proof Policy Q&A",
    conversation_message_field="question",
)


def test_scheduler_uses_optional_task_model_without_exposing_it_to_agent_prompt():
    task = TaskEntity(
        task_type="proof.qa.chat",
        input_payload_json={
            "question": "采购审批要求是什么？",
            "top_k": 3,
            "model_id": " deepseek-v4-flash ",
        },
        user_id="user-1",
        tenant_id="tenant-1",
    )

    message, context = _build_scheduler_input(task, PROOF_QA)

    assert _selected_model_id(task) == "deepseek-v4-flash"
    assert message == "采购审批要求是什么？"
    assert '"top_k": 3' in context
    assert "model_id" not in context


def test_scheduler_uses_agent_default_when_task_model_is_absent():
    task = TaskEntity(
        task_type="proof.qa.chat",
        input_payload_json={"question": "采购审批要求是什么？"},
        user_id="user-1",
        tenant_id="tenant-1",
    )

    assert _selected_model_id(task) is None
