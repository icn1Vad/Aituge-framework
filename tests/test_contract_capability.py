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


def _registered(ir_engine="legacy"):
    registry = CapturingRegistry()
    asyncio.run(
        capability.register(
            registry,
            CapabilitySettings(
                {
                    "CONTRACT_SERVICE_BASE_URL": "http://ai-contract:18200",
                    "CONTRACT_RESULT_SINK_INTERNAL_TOKEN": "callback-secret",
                    "CONTRACT_MODEL_ID": "contract-model",
                    "CONTRACT_IR_ENGINE": ir_engine,
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
    assert pipeline["max_parallelism"] == 7
    stages = {item["stage_id"]: item for item in pipeline["stages"]}
    assert list(stages) == [
        "parse_contract",
        "resolve_parties",
        *capability.IR_FRAGMENT_STAGE_IDS,
        "extract_contract_ir",
        "finalize_review",
    ]
    fragments = set(capability.IR_FRAGMENT_STAGE_IDS)
    assert all(
        stages[item]["depends_on"] == ["parse_contract", "resolve_parties"]
        for item in fragments
    )
    assert stages["extract_contract_ir"]["stage_type"] == "finalizer"
    assert stages["extract_contract_ir"]["depends_on"] == list(capability.IR_FRAGMENT_STAGE_IDS)
    assert stages["extract_contract_ir"]["service_handler"] == "contract_ir_fragment_merge_v1"
    assert all(
        set(stages[item]["tools"]) == {
            "contract_get_document",
            "contract_get_blocks",
            "contract_get_clause_context",
            "contract_get_ir",
        }
        for item in fragments | {"resolve_parties"}
    )
    assert all(
        "required_result_sink_failed" in stages[item]["retry_policy"]["retry_on"]
        for item in fragments | {"resolve_parties"}
    )
    assert stages["finalize_review"]["depends_on"] == [
        "parse_contract",
        "resolve_parties",
        "extract_contract_ir",
    ]
    assert stages["finalize_review"]["service_handler"] == "contract_direct_review_v1"
    assert stages["finalize_review"]["output_model"] is capability.FinalizeReviewStageResult


def test_contract_capability_can_switch_only_ir_stage_to_window_engine() -> None:
    registry = _registered("window")
    stages = {item["stage_id"]: item for item in registry.pipelines[0]["stages"]}

    assert not set(capability.IR_FRAGMENT_STAGE_IDS) & set(stages)
    assert stages["extract_contract_ir"]["depends_on"] == ["parse_contract", "resolve_parties"]
    assert stages["extract_contract_ir"]["service_handler"] == "contract_ir_window_v1"
    assert stages["extract_contract_ir"]["output_model"] is capability.ExtractContractIrStageResult
    assert [item["name"] for item in registry.stage_handlers] == [
        "contract_stage_gateway_v1",
        "contract_ir_fragment_merge_v1",
        "contract_ir_window_v1",
        "contract_direct_review_v1",
    ]
    assert stages["finalize_review"]["depends_on"] == [
        "parse_contract",
        "resolve_parties",
        "extract_contract_ir",
    ]
    assert stages["finalize_review"]["service_handler"] == "contract_direct_review_v1"


def test_contract_capability_rejects_unknown_ir_engine() -> None:
    with pytest.raises(ValueError, match="CONTRACT_IR_ENGINE"):
        _registered("unknown")


def test_direct_final_stage_builds_formal_result_without_legacy_review_stages(
    monkeypatch,
) -> None:
    import services.contract.scripts.contract_risk_stage66_direct_e2e as direct_script

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
                            "text": "测试合同",
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

    class Payload:
        def model_dump(self, **_kwargs):
            return {
                "schema_version": "1.0",
                "review_id": "review-1",
                "business_task_id": "business-1",
                "contract_version_id": "version-1",
                "contract_profile": {
                    "contract_type": "SERVICE",
                    "party_a": {"name": "甲方"},
                    "party_b": {"name": "乙方"},
                    "perspective": "PARTY_A",
                    "our_party": "甲方",
                    "counterparty": "乙方",
                    "review_attitude": "NEUTRAL",
                },
                "summary": {
                    "overview": "未发现需要人工复核的实质合同风险。",
                    "high_count": 0,
                    "medium_count": 0,
                    "low_count": 0,
                    "info_count": 0,
                },
                "findings": [],
                "evidences": [],
                "relationships": [],
                "result_hash": "sha256:" + "1" * 64,
            }

    captured = {}

    async def fake_execute_one(**kwargs):
        captured.update(kwargs)
        return (
            {
                "total_model_calls": 10,
                "total_repairs": 0,
                "total_tool_calls": 0,
                "core_result_signature": "sha256:" + "2" * 64,
            },
            {},
            Payload(),
            object(),
            object(),
        )

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
    monkeypatch.setattr(direct_script, "_execute_one", fake_execute_one)
    task_input = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "attempt_no": 1,
        "business_task_id": "business-1",
        "contract_version_id": "version-1",
        "document_id": "document-1",
        "perspective": "PARTY_A",
        "our_party_name": "甲方",
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
                    "contract_type": "SERVICE",
                    "party_a": {"name": "甲方"},
                    "party_b": {"name": "乙方"},
                    "perspective": "PARTY_A",
                    "our_party": "甲方",
                    "counterparty": "乙方",
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
            "http://ai-contract:18200",
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
    assert captured["diagnostic_allow_oracle_drift"] is True


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


def test_ir_fragments_cover_every_semantic_field_once_and_merge_deterministically() -> None:
    covered = [
        field
        for stage_id in capability.IR_FRAGMENT_STAGE_IDS
        for field in capability.IR_FRAGMENT_FIELDS[stage_id]
    ]
    assert len(covered) == len(set(covered))
    assert set(covered) == set(capability.ContractIrSemanticDelta.model_fields)
    anchor = {
        "anchor_id": "anchor-1",
        "block_id": "block-1",
        "page_number": None,
        "char_start": 0,
        "char_end": 4,
    }
    semantic = {
        "definitions": [{"term": "甲方", "meaning": "委托方", "source_anchors": [anchor]}],
        "dates": [],
        "amounts": [],
        "rights": [],
        "obligations": [
            {
                "item_id": "obligation-1",
                "subject": "乙方",
                "predicate": "交付成果",
                "object": None,
                "source_anchors": [anchor],
            }
        ],
        "prohibitions": [],
        "payment_terms": [],
        "delivery_terms": [],
        "acceptance_terms": [],
        "liabilities": [],
        "termination_terms": [],
        "confidentiality_terms": [],
        "intellectual_property_terms": [],
        "dispute_resolution": [],
    }
    result_types = {
        "extract_ir_definitions_basics": "CONTRACT_IR_DEFINITIONS_BASICS_FRAGMENT_V1",
        "extract_ir_rights_duties": "CONTRACT_IR_RIGHTS_DUTIES_FRAGMENT_V1",
        "extract_ir_commercial_terms": "CONTRACT_IR_COMMERCIAL_TERMS_FRAGMENT_V1",
        "extract_ir_liability_termination": "CONTRACT_IR_LIABILITY_TERMINATION_FRAGMENT_V1",
        "extract_ir_special_terms": "CONTRACT_IR_SPECIAL_TERMS_FRAGMENT_V1",
    }
    artifacts = {
        stage_id: SimpleNamespace(
            content_json={
                "result_type": result_types[stage_id],
                **{field: semantic[field] for field in capability.IR_FRAGMENT_FIELDS[stage_id]},
            }
        )
        for stage_id in capability.IR_FRAGMENT_STAGE_IDS
    }

    merged = asyncio.run(
        capability._merge_contract_ir_fragments_handler()(SimpleNamespace(artifacts=artifacts))
    )

    assert merged.output["result_type"] == "CONTRACT_IR_STAGE_V1"
    assert merged.output["semantic_ir"] == semantic


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


def test_ir_fragment_rejects_duplicate_semantic_items_before_merge() -> None:
    anchor = {
        "anchor_id": "anchor-1",
        "block_id": "block-1",
        "page_number": None,
        "char_start": 0,
        "char_end": 4,
    }
    duplicate = {
        "item_id": "payment-1",
        "subject": "甲方",
        "predicate": "支付服务费",
        "object": "合同签订后支付",
        "source_anchors": [anchor],
    }

    with pytest.raises(ValueError, match="duplicate 'payment_terms' item"):
        capability._fragment_result(
            "extract_ir_commercial_terms",
            {
                "result_type": "CONTRACT_IR_COMMERCIAL_TERMS_FRAGMENT_V1",
                "payment_terms": [
                    duplicate,
                    {**duplicate, "item_id": "payment-2"},
                ],
                "delivery_terms": [],
                "acceptance_terms": [],
            },
        )


def test_internal_ir_fragment_sink_validates_anchors_without_external_callback(monkeypatch) -> None:
    calls = []

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
                            "page_number": None,
                            "paragraph_no": 1,
                            "char_start": 0,
                            "char_end": 4,
                            "text": "甲方付款",
                            "heading_path": [],
                            "metadata": {},
                        }
                    ],
                },
                "request_id": "fragment-validation",
            }

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://ai-contract:18200"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, path, *, headers, json):
            calls.append((path, headers, json))
            return Response()

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
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
    output = {
        "result_type": "CONTRACT_IR_DEFINITIONS_BASICS_FRAGMENT_V1",
        "definitions": [
            {
                "term": "甲方",
                "meaning": "付款方",
                "source_anchors": [
                    {
                        "anchor_id": "anchor-1",
                        "block_id": "block-1",
                        "page_number": None,
                        "char_start": 0,
                        "char_end": 2,
                    }
                ],
            }
        ],
        "dates": [],
        "amounts": [],
    }
    handler = capability._result_sink_handler("http://ai-contract:18200", "secret")

    asyncio.run(
        handler(
            ResultSinkDelivery(
                task,
                SimpleNamespace(result_sink_url=None),
                output,
                "extract_ir_definitions_basics",
                "completed",
                None,
            )
        )
    )

    assert [item[0] for item in calls] == ["/v1/internal/contract-tools/blocks"]
    assert calls[0][2]["block_ids"] == ["block-1"]


def test_internal_ir_fragment_sink_rejects_anchor_outside_the_source_block(monkeypatch) -> None:
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
                            "text": "甲方付款",
                            "heading_path": [],
                            "metadata": {},
                        }
                    ],
                },
                "request_id": "fragment-validation",
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

    monkeypatch.setattr(capability.httpx, "AsyncClient", Client)
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
        {
            "result_type": "CONTRACT_IR_DEFINITIONS_BASICS_FRAGMENT_V1",
            "definitions": [
                {
                    "term": "甲方",
                    "meaning": "付款方",
                    "source_anchors": [
                        {
                            "anchor_id": "anchor-1",
                            "block_id": "block-1",
                            "page_number": 1,
                            "char_start": 0,
                            "char_end": 5,
                        }
                    ],
                }
            ],
            "dates": [],
            "amounts": [],
        },
        "extract_ir_definitions_basics",
        "completed",
        None,
    )

    with pytest.raises(capability.ResultSinkRejectedError) as caught:
        asyncio.run(capability._result_sink_handler("http://ai-contract:18200", "secret")(delivery))

    assert caught.value.code == "RESULT_INVALID"
    assert caught.value.retryable is True


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
    _, ir_fragment = capability._callback_envelope(
        ResultSinkDelivery(
            task=task,
            definition=definition,
            output=None,
            stage_id="extract_ir_commercial_terms",
            status="failed",
            error_message="fragment output was truncated",
            error_code="invalid_output",
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
    assert ir_fragment["stage_id"] == "extract_contract_ir"
    assert ir_fragment["error"]["code"] == "FRAMEWORK_RUN_FAILED"
    assert ir_fragment["error"]["details"]["internal_stage_id"] == "extract_ir_commercial_terms"


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
