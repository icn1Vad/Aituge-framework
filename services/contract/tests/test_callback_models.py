from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from contract.callback.models import FrameworkCallback


ADAPTER = TypeAdapter(FrameworkCallback)


def _base() -> dict:
    return {
        "schema_version": "1.0",
        "review_id": "review-1",
        "attempt_no": 1,
        "framework_task_id": "task-1",
        "framework_run_id": "run-1",
        "event_sequence": 10,
        "callback_id": "callback-1",
    }


def test_stage_result_callback_uses_nested_result_discriminator() -> None:
    value = ADAPTER.validate_python(
        {
            **_base(),
            "callback_type": "STAGE_RESULT",
            "stage_id": "parse_contract",
            "result": {
                "result_type": "PARSE_CONTRACT_STAGE_V1",
                "document_id": "document-1",
                "generation_id": "generation-1",
                "block_count": 3,
                "ir_hash": "sha256:" + "1" * 64,
            },
            "error": None,
        }
    )

    assert value.callback_type == "STAGE_RESULT"
    assert value.result.result_type == "PARSE_CONTRACT_STAGE_V1"


def test_review_stage_accepts_a_text_evidence_candidate_without_a_model_generated_hash() -> None:
    value = ADAPTER.validate_python(
        {
            **_base(),
            "callback_type": "STAGE_RESULT",
            "stage_id": "rights_obligations_review",
            "result": {
                "result_type": "RIGHTS_OBLIGATIONS_STAGE_V1",
                "findings": [],
                "evidences": [
                    {
                        "evidence_id": "evidence-1",
                        "finding_id": "finding-1",
                        "evidence_type": "TEXT_QUOTE",
                        "block_id": "block-1",
                        "char_start": 0,
                        "char_end": 8,
                    }
                ],
            },
            "error": None,
        }
    )

    assert value.result.evidences[0].quoted_text_hash is None


def test_stage_and_result_type_must_match() -> None:
    with pytest.raises(ValidationError, match="requires result_type"):
        ADAPTER.validate_python(
            {
                **_base(),
                "callback_type": "STAGE_RESULT",
                "stage_id": "resolve_parties",
                "result": {
                    "result_type": "PARSE_CONTRACT_STAGE_V1",
                    "document_id": "document-1",
                    "generation_id": "generation-1",
                    "block_count": 3,
                    "ir_hash": "sha256:" + "1" * 64,
                },
                "error": None,
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            **_base(),
            "callback_type": "RUN_SUCCEEDED",
            "stage_id": "finalize_review",
            "result": None,
            "error": None,
        },
        {
            **_base(),
            "callback_type": "RUN_FAILED",
            "stage_id": None,
            "result": None,
            "error": None,
        },
    ],
)
def test_terminal_callback_nullability_is_strict(payload) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(payload)
