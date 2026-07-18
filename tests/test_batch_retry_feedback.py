from __future__ import annotations

from types import SimpleNamespace

from task_manager.handlers.batch_item_scheduler import (
    _build_item_message,
    _validation_retry_feedback,
)


def test_batch_retry_message_includes_concise_validation_feedback() -> None:
    feedback = _validation_retry_feedback(
        {
            "errors": [
                {
                    "loc": ["findings", 0, "candidate_ids"],
                    "msg": "Every candidate ID must have evidence.",
                }
            ]
        }
    )
    message = _build_item_message(
        SimpleNamespace(task_type="proof.audit.run", title="Proof", input_payload_json={}),
        SimpleNamespace(name="Proof"),
        SimpleNamespace(input_payload_json={"id": "item-1"}),
        retry_feedback=feedback,
    )

    assert "Previous response validation feedback:" in message
    assert "findings.0.candidate_ids" in message
    assert "Every candidate ID must have evidence." in message
    assert "do not repeat the invalid response" in message
