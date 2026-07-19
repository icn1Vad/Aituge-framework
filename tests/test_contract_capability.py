import asyncio
from types import SimpleNamespace

import httpx
import pytest

from capability_mount import CapabilitySettings
from services.contract.capabilities import register as capability
from task_manager.result_sink import ResultSinkDelivery


class CapturingRegistry:
    def __init__(self) -> None:
        self.skill_roots = []
        self.tools = []
        self.skill_packages = []
        self.agents = []
        self.stage_handlers = []
        self.result_sinks = []
        self.tasks = []
        self.pipelines = []

    def register_skill_root(self, value):
        self.skill_roots.append(value)

    def register_http_tool(self, **value):
        self.tools.append(value)

    def register_skill_package(self, **value):
        self.skill_packages.append(value)

    def register_agent(self, **value):
        self.agents.append(value)

    def register_stage_handler(self, **value):
        self.stage_handlers.append(value)

    def register_result_sink(self, **value):
        self.result_sinks.append(value)

    def register_task(self, **value):
        self.tasks.append(value)

    def register_pipeline(self, **value):
        self.pipelines.append(value)


def _registered():
    registry = CapturingRegistry()
    asyncio.run(
        capability.register(
            registry,
            CapabilitySettings(
                {
                    "CONTRACT_SERVICE_BASE_URL": "http://ai-contract:18200",
                    "CONTRACT_RESULT_SINK_INTERNAL_TOKEN": "callback-secret",
                    "CONTRACT_MODEL_ID": "contract-model",
                }
            ),
        )
    )
    return registry


def test_contract_capability_registers_frozen_pipeline_and_internal_tools() -> None:
    registry = _registered()

    assert [item["tool_name"] for item in registry.tools] == [
        "contract_get_document",
        "contract_get_blocks",
        "contract_get_clause_context",
        "contract_get_ir",
    ]
    assert all(item["headers"]["X-Internal-Token"] == "callback-secret" for item in registry.tools)
    assert all(item["request_id_header"] == "X-Request-Id" for item in registry.tools)
    assert len(registry.skill_packages) == 8
    assert registry.agents[0]["agent_id"] == "contract-review-neutral-v1"
    assert registry.tasks[0]["task_type"] == "contract.review.run"
    assert registry.tasks[0]["pipeline_id"] == "contract-review-pipeline-v1"
    assert registry.result_sinks[0]["task_type"] == "contract.review.run"
    assert registry.result_sinks[0]["required"] is True

    pipeline = registry.pipelines[0]
    assert pipeline["pipeline_id"] == "contract-review-pipeline-v1"
    assert pipeline["max_parallelism"] == 5
    stages = {item["stage_id"]: item for item in pipeline["stages"]}
    assert list(stages) == list(capability.STAGE_SEQUENCE)
    parallel = {
        "rights_obligations_review",
        "commercial_terms_review",
        "liability_termination_review",
        "missing_ambiguous_clauses",
        "relation_extraction",
    }
    assert all(stages[item]["depends_on"] == ["extract_contract_ir"] for item in parallel)
    assert all(stages[item]["input_model"] is capability.ContractTaskInput for item in parallel)
    assert all(stages[item]["input_adapter"] == "task_input" for item in parallel)
    assert all(
        set(stages[item]["tools"]) == {
            "contract_get_document",
            "contract_get_blocks",
            "contract_get_clause_context",
            "contract_get_ir",
        }
        for item in parallel | {"resolve_parties", "extract_contract_ir"}
    )
    assert all(
        "required_result_sink_failed" in stages[item]["retry_policy"]["retry_on"]
        for item in parallel | {"resolve_parties", "extract_contract_ir"}
    )
    assert parallel < set(stages["verify_evidence"]["depends_on"])
    assert stages["finalize_review"]["depends_on"] == ["verify_evidence"]


def test_review_stages_use_source_candidates_and_stage_specific_contract_rules() -> None:
    registry = _registered()
    assert "Never invent" in registry.agents[0]["system_prompt"]
    assert "Evidence Candidates" in registry.agents[0]["system_prompt"]
    assert "at most four highest-materiality findings" in registry.agents[0]["system_prompt"]
    assert "For ABSENCE evidence" in registry.agents[0]["system_prompt"]

    review_skills = {
        "contract-rights-obligations": "RIGHTS_OBLIGATIONS_IMBALANCE",
        "contract-commercial-terms": "PAYMENT",
        "contract-liability-termination": "LIABILITY",
        "contract-missing-ambiguity": "ABSENCE",
        "contract-relation-extraction": "internal_relationships",
    }
    for skill_name, domain_rule in review_skills.items():
        content = (
            capability.CAPABILITY_DIR / "skills" / skill_name / "SKILL.md"
        ).read_text("utf-8")
        normalized = " ".join(content.split())
        assert "contract_get_ir" in content
        assert "review_id" in content and "document_id" in content
        assert "quoted_text_hash" in content
        assert "Contract Python" in normalized
        assert domain_rule in content
        assert "at most four highest-materiality findings" in content
        assert "Start the final answer immediately" in normalized

    candidate_schema = capability.RightsObligationsStageResult.model_json_schema()["$defs"][
        "EvidenceCandidate"
    ]
    assert "block_id" in candidate_schema["properties"]
    assert "quoted_text_hash" not in candidate_schema["required"]


def test_ir_extraction_returns_only_a_semantic_delta() -> None:
    schema = capability.ExtractContractIrStageResult.model_json_schema()
    assert set(schema["properties"]) == {"result_type", "semantic_ir"}
    semantic_schema = schema["$defs"]["ContractIrSemanticDelta"]
    assert "rights" in semantic_schema["properties"]
    assert not {
        "document",
        "clauses",
        "parties",
        "our_party",
        "counterparty",
        "contract_type",
        "source_anchors",
    } & set(semantic_schema["properties"])

    content = (
        capability.CAPABILITY_DIR / "skills" / "contract-ir-extraction" / "SKILL.md"
    ).read_text("utf-8")
    assert "semantic_ir" in content
    assert "Contract Python deterministically composes" in content
    assert "Do not return or rewrite `document`" in content


def test_contract_result_sink_emits_three_frozen_callback_shapes(monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {"accepted": True, "duplicate": False, "ignored_reason": None},
                "request_id": "callback-request",
            }

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://ai-contract:18200"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, *, headers, json):
            calls.append((path, headers, json))
            return Response()

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
    handler = capability._result_sink_handler("http://ai-contract:18200", "secret")
    task_input = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "attempt_no": 1,
        "business_task_id": "business-1",
        "contract_version_id": "version-1",
        "document_id": "document-1",
        "perspective": "PARTY_A",
        "our_party_name": None,
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
    }
    task = SimpleNamespace(
        id="task-1",
        current_run_id="run-1",
        task_type="contract.review.run",
        input_payload_json=task_input,
    )
    definition = SimpleNamespace(result_sink_url=None)

    asyncio.run(
        handler(
            ResultSinkDelivery(
                task,
                definition,
                {"result_type": "PARSE_CONTRACT_STAGE_V1"},
                "parse_contract",
                "completed",
                None,
            )
        )
    )
    asyncio.run(handler(ResultSinkDelivery(task, definition, {"ignored": True}, None, "completed", None)))
    long_error = "bad" * 1000
    asyncio.run(
        handler(ResultSinkDelivery(task, definition, None, "resolve_parties", "failed", long_error))
    )

    assert [item[2]["callback_type"] for item in calls] == [
        "STAGE_RESULT",
        "RUN_SUCCEEDED",
        "RUN_FAILED",
    ]
    assert calls[0][2]["stage_id"] == "parse_contract"
    assert calls[0][2]["result"] == {"result_type": "PARSE_CONTRACT_STAGE_V1"}
    assert calls[1][2]["stage_id"] is None and calls[1][2]["result"] is None
    assert calls[1][2]["error"] is None
    assert calls[2][2]["result"] is None
    assert calls[2][2]["error"]["code"] == "FRAMEWORK_RUN_FAILED"
    assert len(calls[2][2]["error"]["message"]) == 2000
    assert all(item[1]["X-Internal-Token"] == "secret" for item in calls)


def test_contract_result_sink_preserves_safe_rejection_detail(monkeypatch) -> None:
    class Response:
        status_code = 422
        text = '{"error":{"code":"RESULT_INVALID","message":"source anchor rejected"}}'

        def raise_for_status(self):
            request = httpx.Request("POST", "http://ai-contract:18200/callback")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("rejected", request=request, response=response)

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
    handler = capability._result_sink_handler("http://ai-contract:18200", "secret")
    task = SimpleNamespace(
        id="task-1",
        current_run_id="run-1",
        input_payload_json={
            "schema_version": "1.0",
            "review_id": "review-1",
            "attempt_no": 1,
            "business_task_id": "business-1",
            "contract_version_id": "version-1",
            "document_id": "document-1",
            "perspective": "PARTY_A",
            "our_party_name": None,
            "contract_type": "AUTO",
            "review_attitude": "NEUTRAL",
        },
    )
    delivery = ResultSinkDelivery(
        task,
        SimpleNamespace(result_sink_url=None),
        {"result_type": "PARSE_CONTRACT_STAGE_V1"},
        "parse_contract",
        "completed",
        None,
    )

    with pytest.raises(capability.ResultSinkRejectedError, match="source anchor rejected"):
        asyncio.run(handler(delivery))


def test_contract_stage_gateway_preserves_safe_rejection_detail(monkeypatch) -> None:
    class Response:
        status_code = 422
        text = '{"error":{"code":"RESULT_INVALID","message":"duplicate finding id"}}'

        def raise_for_status(self):
            request = httpx.Request("POST", "http://ai-contract:18200/stage")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("rejected", request=request, response=response)

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
    handler = capability._stage_gateway_handler("http://ai-contract:18200", "secret")
    task_input = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "attempt_no": 1,
        "business_task_id": "business-1",
        "contract_version_id": "version-1",
        "document_id": "document-1",
        "perspective": "PARTY_A",
        "our_party_name": None,
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
    }
    context = SimpleNamespace(
        task=SimpleNamespace(id="task-1", input_payload_json=task_input),
        run=SimpleNamespace(id="run-1"),
        stage=SimpleNamespace(stage_id="verify_evidence"),
        stage_input={"artifacts": {}},
    )

    with pytest.raises(RuntimeError, match="duplicate finding id"):
        asyncio.run(handler(context))
