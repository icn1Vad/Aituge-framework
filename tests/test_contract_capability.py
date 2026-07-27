import asyncio
from types import SimpleNamespace

import httpx
import pytest

from capability_mount import CapabilitySettings
from services.contract.capabilities import register as capability
from task_manager.pipeline.errors import StageExecutionError
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
        "contract_get_review_result",
    ]
    assert all(item["headers"]["X-Internal-Token"] == "callback-secret" for item in registry.tools)
    assert all(item["request_id_header"] == "X-Request-Id" for item in registry.tools)
    assert len(registry.skill_packages) == 3
    assert registry.agents[0]["agent_id"] == "contract-review-neutral-v1"
    assert registry.tasks[0]["task_type"] == "contract.review.run"
    assert registry.tasks[0]["pipeline_id"] == "contract-review-pipeline-v1"
    assert registry.result_sinks[0]["task_type"] == "contract.review.run"
    assert registry.result_sinks[0]["required"] is True

    grounded_task = next(
        item for item in registry.tasks if item["task_type"] == "contract.grounded.answer"
    )
    grounded_agent = next(
        item
        for item in registry.agents
        if item["agent_id"] == "contract-grounded-answer-v1"
    )
    grounded_pipeline = next(
        item
        for item in registry.pipelines
        if item["pipeline_id"] == "contract-grounded-answer-pipeline-v1"
    )
    grounded_stages = {
        item["stage_id"]: item for item in grounded_pipeline["stages"]
    }
    assert grounded_task["handler"] == "pipeline"
    assert grounded_task["pipeline_id"] == "contract-grounded-answer-pipeline-v1"
    assert grounded_agent["default_tools"] == ["contract_get_review_result"]
    assert grounded_stages["generate_grounded_answer"]["input_adapter"] == "task_input"
    assert grounded_stages["finalize_grounded_answer"]["service_handler"] == (
        "contract_grounded_answer_finalize_v1"
    )

    pipeline = registry.pipelines[0]
    assert pipeline["pipeline_id"] == "contract-review-pipeline-v1"
    assert pipeline["max_parallelism"] == 7
    stages = {item["stage_id"]: item for item in pipeline["stages"]}
    assert list(stages) == [
        "parse_contract",
        "resolve_parties",
        "extract_contract_ir",
        "finalize_review",
    ]
    assert stages["extract_contract_ir"]["stage_type"] == "finalizer"
    assert stages["extract_contract_ir"]["depends_on"] == ["parse_contract", "resolve_parties"]
    assert stages["extract_contract_ir"]["service_handler"] == "contract_ir_window_v1"
    assert stages["extract_contract_ir"]["output_model"] is capability.ExtractContractIrStageResult
    assert [item["name"] for item in registry.stage_handlers] == [
        "contract_stage_gateway_v1",
        "contract_ir_window_v1",
        "contract_direct_review_v1",
        "contract_grounded_answer_finalize_v1",
    ]
    assert stages["finalize_review"]["depends_on"] == [
        "parse_contract",
        "resolve_parties",
        "extract_contract_ir",
    ]
    assert stages["finalize_review"]["service_handler"] == "contract_direct_review_v1"


def test_legacy_ir_execution_path_is_removed() -> None:
    legacy_stage_ids = {
        "extract_ir_definitions_basics",
        "extract_ir_rights_duties",
        "extract_ir_commercial_terms",
        "extract_ir_liability_termination",
        "extract_ir_special_terms",
    }
    registry = _registered()
    pipeline = registry.pipelines[0]
    assert not legacy_stage_ids & {
        stage["stage_id"] for stage in pipeline["stages"]
    }
    assert "contract_ir_fragment_merge_v1" not in {
        handler["name"] for handler in registry.stage_handlers
    }
    assert not hasattr(capability, "IR_FRAGMENT_STAGE_IDS")
    assert not hasattr(capability, "_merge_contract_ir_fragments_handler")
    assert not any(
        path.name.startswith("contract-ir-")
        for path in (capability.CAPABILITY_DIR / "skills").iterdir()
    )


def test_direct_final_stage_builds_formal_result_without_legacy_review_stages(
    monkeypatch,
) -> None:
    from services.contract.capabilities import direct_runtime

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {
                    "review_id": "review-1",
                    "document_id": "document-1",
                    "generation_id": "generation-1",
                    "blocks": [
                        {
                            "block_id": "block-1",
                            "block_no": 1,
                            "block_type": "paragraph",
                            "page_number": 1,
                            "paragraph_no": 1,
                            "char_start": 0,
                            "char_end": 4,
                            "text": "Test",
                            "heading_path": [],
                            "metadata": {},
                        }
                    ],
                },
                "request_id": "request-1",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    captured = {}

    async def fake_execute_direct_bundle(**kwargs):
        captured.update(kwargs)
        return (
            SimpleNamespace(
                status="COMPLETED",
                review_id="review-1",
                findings=[],
            ),
            {
                "total_model_calls": 0,
                "total_repairs": 0,
                "total_tool_calls": 0,
                "core_result_signature": "sha256:" + "2" * 64,
            },
        )

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
    monkeypatch.setattr(direct_runtime, "execute_direct_bundle", fake_execute_direct_bundle)
    task_input = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "attempt_no": 1,
        "business_task_id": "business-1",
        "contract_version_id": "version-1",
        "document_id": "document-1",
        "perspective": "PARTY_A",
        "our_party_name": "Party A",
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
    }
    empty_ir = {
        field: [] for field in capability.ContractIrSemanticDelta.model_fields
    }
    context = SimpleNamespace(
        task=SimpleNamespace(
            id="task-1",
            tenant_id="tenant-1",
            input_payload_json=task_input,
        ),
        run=SimpleNamespace(id="run-1"),
        artifacts={
            "parse_contract": SimpleNamespace(
                content_json={
                    "result_type": "PARSE_CONTRACT_STAGE_V1",
                    "document_id": "document-1",
                    "generation_id": "generation-1",
                    "block_count": 1,
                    "ir_hash": "sha256:" + "0" * 64,
                }
            ),
            "resolve_parties": SimpleNamespace(
                content_json={
                    "result_type": "PARTY_RESOLUTION_STAGE_V1",
                    "contract_type": "AUTO",
                    "party_a": {"name": "Party A"},
                    "party_b": {"name": "Party B"},
                    "perspective": "PARTY_A",
                    "our_party": "Party A",
                    "counterparty": "Party B",
                }
            ),
            "extract_contract_ir": SimpleNamespace(
                content_json={
                    "result_type": "CONTRACT_IR_STAGE_V1",
                    "semantic_ir": empty_ir,
                }
            ),
        },
    )

    result = asyncio.run(
        capability._direct_contract_review_handler(
            "http://framework:8894",
            "callback-secret",
            "contract-model",
        )(context)
    )

    assert result.output["result_type"] == "FINAL_REVIEW_STAGE_V1"
    assert result.output["review_id"] == "review-1"
    assert result.metadata["risk_review_engine"] == "direct"
    assert result.metadata["review_unit_count"] == 7
    assert result.metadata["check_count"] == 45
    assert captured["allow_dynamic_base_batch_count"] is True


def test_legacy_react_review_stages_and_skills_are_not_registered() -> None:
    registry = _registered()
    registered_stages = {
        stage["stage_id"] for stage in registry.pipelines[0]["stages"]
    }
    legacy_stages = {
        "rights_obligations_review",
        "commercial_terms_review",
        "liability_termination_review",
        "missing_ambiguous_clauses",
        "relation_extraction",
        "verify_evidence",
    }
    assert not registered_stages & legacy_stages
    registered_skills = {
        item["primary_skill"] for item in registry.skill_packages
    }
    assert not {
        "contract-rights-obligations",
        "contract-commercial-terms",
        "contract-liability-termination",
        "contract-missing-ambiguity",
        "contract-relation-extraction",
    } & registered_skills


def test_window_ir_returns_only_a_semantic_delta() -> None:
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

def test_window_ir_handler_injects_party_context_without_changing_source(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {
                    "review_id": "review-1",
                    "document_id": "document-1",
                    "generation_id": "generation-1",
                    "expected_blocks": [{"block_id": "block-1", "text_length": 4}],
                    "expected_section_ids": ["section-1"],
                    "windows": [
                        {
                            "window_id": "window-1",
                            "sequence_no": 1,
                            "section_ids": ["section-1"],
                            "heading_path": [],
                            "clause_nos": [],
                            "primary_block_ids": ["block-1"],
                            "estimated_tokens": 4,
                            "source_text": "test",
                            "context_text": "section context",
                            "offset_map": [
                                {
                                    "rendered_start": 0,
                                    "rendered_end": 4,
                                    "block_id": "block-1",
                                    "block_no": 1,
                                    "block_char_start": 0,
                                    "block_char_end": 4,
                                    "page_number": None,
                                }
                            ],
                        }
                    ],
                    "concurrency": 10,
                },
                "request_id": "window-plan",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, path, *, headers, json):
            captured["http"] = (path, headers, json)
            return Response()

    class FakePipeline:
        async def run(self, request, *, tenant_id, model_id):
            captured["request"] = request
            captured["tenant_id"] = tenant_id
            captured["model_id"] = model_id
            semantic = capability.ContractIrSemanticDelta()
            return SimpleNamespace(
                semantic_ir=semantic,
                duration_ms=12,
                model_call_count=1,
                retry_count=0,
                semantic_ir_hash="sha256:" + "1" * 64,
            )

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
    monkeypatch.setattr(capability, "ContractIrWindowPipeline", lambda **_kwargs: FakePipeline())
    context = SimpleNamespace(
        task=SimpleNamespace(
            tenant_id="tenant-1",
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
        ),
        run=SimpleNamespace(id="run-1"),
        stage=SimpleNamespace(stage_id="extract_contract_ir"),
        artifacts={
            "resolve_parties": SimpleNamespace(
                content_json={
                    "result_type": "PARTY_RESOLUTION_STAGE_V1",
                    "contract_type": "SERVICE",
                    "party_a": {"name": "Party A"},
                    "party_b": {"name": "Party B"},
                    "perspective": "PARTY_A",
                    "our_party": "Party A",
                    "counterparty": "Party B",
                }
            )
        },
    )

    result = asyncio.run(
        capability._window_contract_ir_handler(
            "http://ai-contract:18200", "secret", "contract-model"
        )(context)
    )

    assert result.output == {
        "result_type": "CONTRACT_IR_STAGE_V1",
        "semantic_ir": capability.ContractIrSemanticDelta().model_dump(mode="json"),
    }
    assert captured["http"][0] == "/v1/internal/contract-tools/windows"
    assert captured["request"].windows[0].source_text == "test"
    assert "PARTY_A_NAME=Party A" in captured["request"].windows[0].context_text
    assert "section context" in captured["request"].windows[0].context_text
    assert captured["tenant_id"] == "tenant-1"
    assert captured["model_id"] == "contract-model"


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


def test_contract_failed_callback_maps_stable_business_errors() -> None:
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
            "perspective": "PARTY_B",
            "our_party_name": "Beta Company",
            "contract_type": "AUTO",
            "review_attitude": "NEUTRAL",
        },
    )
    definition = SimpleNamespace(result_sink_url=None)

    _, party = capability._callback_envelope(
        ResultSinkDelivery(
            task=task,
            definition=definition,
            output=None,
            stage_id="resolve_parties",
            status="failed",
            error_message="party result rejected",
            error_code="required_result_sink_failed",
            retryable=True,
            domain_error_code="PARTY_UNRESOLVED",
            domain_retryable=False,
            user_action_required=True,
            error_details={
                "candidate_parties": ["Acme Company", "Beta Company"],
                "requested_our_party_name": "Gamma Company",
            },
        )
    )
    _, evidence = capability._callback_envelope(
        ResultSinkDelivery(
            task=task,
            definition=definition,
            output=None,
            stage_id="verify_evidence",
            status="failed",
            error_message="evidence rejected",
            error_code="EVIDENCE_INVALID",
            retryable=False,
        )
    )
    _, model_failure = capability._callback_envelope(
        ResultSinkDelivery(
            task=task,
            definition=definition,
            output=None,
            stage_id="resolve_parties",
            status="failed",
            error_message="LLM model is unavailable",
            error_code="invalid_output",
            retryable=True,
        )
    )
    _, review_evidence = capability._callback_envelope(
        ResultSinkDelivery(
            task=task,
            definition=definition,
            output=None,
            stage_id="finalize_review",
            status="failed",
            error_message="evidence range rejected",
            error_code="required_result_sink_failed",
            retryable=True,
        )
    )
    assert party["error"] == {
        "code": "PARTY_UNRESOLVED",
        "message": "party result rejected",
        "retryable": False,
        "user_action_required": True,
        "details": {
            "candidate_parties": ["Acme Company", "Beta Company"],
            "requested_our_party_name": "Gamma Company",
            "stage_id": "resolve_parties",
            "framework_error_code": "required_result_sink_failed",
        },
    }
    assert evidence["error"]["code"] == "EVIDENCE_INVALID"
    assert evidence["error"]["retryable"] is False
    assert model_failure["error"]["code"] == "FRAMEWORK_RUN_FAILED"
    assert model_failure["error"]["retryable"] is True
    assert review_evidence["error"]["code"] == "EVIDENCE_INVALID"
    assert review_evidence["error"]["retryable"] is False
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

    with pytest.raises(capability.ResultSinkRejectedError, match="source anchor rejected") as caught:
        asyncio.run(handler(delivery))
    assert caught.value.code == "RESULT_INVALID"


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

    with pytest.raises(StageExecutionError, match="duplicate finding id") as caught:
        asyncio.run(handler(context))
    assert caught.value.code == "RESULT_INVALID"
    assert caught.value.retryable is False
