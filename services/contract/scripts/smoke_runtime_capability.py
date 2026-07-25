from __future__ import annotations

import json
import os
import urllib.request


EXPECTED_STAGES = [
    "parse_contract",
    "resolve_parties",
    "extract_ir_definitions_basics",
    "extract_ir_rights_duties",
    "extract_ir_commercial_terms",
    "extract_ir_liability_termination",
    "extract_ir_special_terms",
    "extract_contract_ir",
    "finalize_review",
]


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def main() -> None:
    framework = os.getenv(
        "FRAMEWORK_SMOKE_BASE_URL",
        "http://framework:8894",
    ).rstrip("/")
    contract = os.getenv(
        "CONTRACT_SMOKE_BASE_URL",
        "http://ai-contract:18200",
    ).rstrip("/")

    health = _get_json(f"{contract}/health")
    assert health["success"] is True
    assert health["data"]["status"] == "UP"
    assert health["data"]["mode"] == "runtime"

    definitions = _get_json(f"{framework}/task-manager/definitions")["definitions"]
    task = next(item for item in definitions if item["task_type"] == "contract.review.run")
    assert task["handler"] == "pipeline"
    assert task["pipeline_id"] == "contract-review-pipeline-v1"

    pipelines = _get_json(f"{framework}/task-manager/pipelines")["pipelines"]
    pipeline = next(
        item for item in pipelines if item["pipeline_id"] == "contract-review-pipeline-v1"
    )
    assert pipeline["task_type"] == "contract.review.run"
    assert pipeline["max_parallelism"] == 7
    assert [stage["stage_id"] for stage in pipeline["stages"]] == EXPECTED_STAGES

    openapi = _get_json(f"{contract}/openapi.json")
    required_paths = {
        "/v1/internal/contract-reviews/{review_id}/framework-result",
        "/v1/internal/contract-reviews/{review_id}/stages/execute",
        "/v1/internal/contract-tools/document",
        "/v1/internal/contract-tools/blocks",
        "/v1/internal/contract-tools/clause-context",
        "/v1/internal/contract-tools/ir",
    }
    assert required_paths <= set(openapi["paths"])
    print("CONTRACT_RUNTIME_SMOKE=OK")
    print(f"TASK_TYPE={task['task_type']}")
    print(f"PIPELINE={pipeline['pipeline_id']}")
    print(f"STAGE_COUNT={len(pipeline['stages'])}")


if __name__ == "__main__":
    main()
