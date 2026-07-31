from __future__ import annotations

from task_manager.handlers.scheduler_task import _compatible_scheduler_tools
from task_manager.models import TaskEntity


PROOF_QA_TOOLS = [
    "proof_search",
    "proof_sql",
    "code_interpreter",
    "html_report_renderer",
]


def _task(task_type: str, model_pack_id: str) -> TaskEntity:
    return TaskEntity(
        task_type=task_type,
        model_pack_id=model_pack_id,
    )


def test_private_local_proof_qa_omits_incompatible_html_report_tool() -> None:
    task_type = "proof.qa.chat"

    tools = _compatible_scheduler_tools(
        _task(task_type, "local-q5"),
        PROOF_QA_TOOLS,
    )

    assert tools == [
        "proof_search",
        "proof_sql",
        "code_interpreter",
    ]


def test_public_api_proof_qa_keeps_html_report_tool() -> None:
    task_type = "proof.qa.chat"

    tools = _compatible_scheduler_tools(
        _task(task_type, "api-rerank"),
        PROOF_QA_TOOLS,
    )

    assert tools == PROOF_QA_TOOLS


def test_other_local_tasks_keep_their_declared_tools() -> None:
    task_type = "contract.review"

    tools = _compatible_scheduler_tools(
        _task(task_type, "local-q5"),
        PROOF_QA_TOOLS,
    )

    assert tools == PROOF_QA_TOOLS
