import json

import pytest
from fastapi.testclient import TestClient

from services.contract.capabilities.window_extraction import (
    WindowExtractionEngine,
    WindowExtractionError,
    WindowExtractionRequest,
    WindowExtractionResult,
)
from services.contract.capabilities.window_extractor_api import create_app


class FakeRuntime:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def extract(
        self,
        window,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ):
        self.calls.append(
            {
                "window": window,
                "tenant_id": tenant_id,
                "model_id": model_id,
                "retry_feedback": retry_feedback,
            }
        )
        return WindowExtractionResult(
            window_id=window.window_id,
            model_id=model_id,
            extractions=[],
        )


def _request(source_text: str | None = None) -> WindowExtractionRequest:
    block_one = "第一条 乙方应在十日内交付成果。"
    block_two = "第二条 甲方应在验收后支付费用。"
    source = source_text or f"{block_one}\n\n{block_two}"
    second_start = len(block_one) + 2
    return WindowExtractionRequest(
        window_id="window-001",
        context_text="双方为甲方和乙方。",
        source_text=source,
        offset_map=[
            {
                "rendered_start": 0,
                "rendered_end": len(block_one),
                "block_id": "block-001",
                "block_no": 1,
                "block_char_start": 0,
                "block_char_end": len(block_one),
            },
            {
                "rendered_start": second_start,
                "rendered_end": second_start + len(block_two),
                "block_id": "block-002",
                "block_no": 2,
                "block_char_start": 0,
                "block_char_end": len(block_two),
            },
        ],
    )


def _single_block_request(source_text: str) -> WindowExtractionRequest:
    return WindowExtractionRequest(
        window_id="window-single",
        source_text=source_text,
        offset_map=[
            {
                "rendered_start": 0,
                "rendered_end": len(source_text),
                "block_id": "block-single",
                "block_no": 1,
                "block_char_start": 0,
                "block_char_end": len(source_text),
            }
        ],
    )


def _model_output(extraction_text: str = "乙方应在十日内交付成果") -> str:
    return json.dumps(
        {
            "extractions": [
                {
                    "extraction_class": "OBLIGATION",
                    "extraction_text": extraction_text,
                    "subject": "乙方",
                    "predicate": "应交付",
                    "object": "成果",
                    "term": None,
                    "meaning": None,
                    "referenced_clause_nos": ["第一条"],
                }
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_window_extractor_uses_framework_runtime_and_exact_block_alignment() -> None:
    runtime = FakeRuntime(_model_output())
    tenants: list[str] = []

    def runtime_factory(tenant_id: str) -> FakeRuntime:
        tenants.append(tenant_id)
        return runtime

    result = await WindowExtractionEngine(runtime_factory=runtime_factory).extract(
        _request(),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert tenants == ["tenant-001"]
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["model_id"] == "contract-model"
    assert call["max_tokens"] == 20_000
    assert call["temperature"] == 0
    assert call["thinking_override"] is False
    assert "source_text" in call["messages"][0]["content"]
    assert "不调用工具" in call["system_prompt"]
    assert "类别不是互斥分类" in call["system_prompt"]
    assert "OBLIGATION 和 PAYMENT" in call["system_prompt"]

    extraction = result.extractions[0]
    expected_text = "乙方应在十日内交付成果"
    expected_start = _request().source_text.index(expected_text)
    expected_end = expected_start + len(expected_text)
    assert extraction.extraction_text == "乙方应在十日内交付成果"
    assert extraction.alignment_status == "MATCH_EXACT"
    assert extraction.rendered_char_start == expected_start
    assert extraction.rendered_char_end == expected_end
    assert len(extraction.source_spans) == 1
    assert extraction.source_spans[0].model_dump() == {
        "block_id": "block-001",
        "block_no": 1,
        "block_char_start": expected_start,
        "block_char_end": expected_end,
        "quoted_text": "乙方应在十日内交付成果",
    }


@pytest.mark.asyncio
async def test_window_extractor_accepts_complete_json_with_explanatory_prefix() -> None:
    response = f"过程文字必须被忽略。\n```json\n{_model_output()}\n```\n结束"
    runtime = FakeRuntime(response)

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _request(),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert len(result.extractions) == 1
    assert result.extractions[0].source_spans[0].block_id == "block-001"


@pytest.mark.asyncio
async def test_window_extractor_rejects_ungrounded_or_paraphrased_text() -> None:
    runtime = FakeRuntime(_model_output("乙方需要尽快交付成果"))

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _request(),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_ALIGNMENT_FAILED"


@pytest.mark.asyncio
async def test_window_extractor_normalizes_layout_but_returns_exact_source_span() -> None:
    source = "乙方应于 ２０２６ 年 ７ 月 １ 日前，支付 １００，０００ 元。"
    runtime = FakeRuntime(_model_output("乙方应于2026年7月1日前支付100000元"))

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    extraction = result.extractions[0]
    assert extraction.alignment_status == "MATCH_NORMALIZED"
    assert extraction.extraction_text == source
    assert extraction.rendered_char_start == 0
    assert extraction.rendered_char_end == len(source)
    assert extraction.source_spans[0].model_dump() == {
        "block_id": "block-single",
        "block_no": 1,
        "block_char_start": 0,
        "block_char_end": len(source),
        "quoted_text": source,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "model_text"),
    [
        ("甲方应支付10万元。", "甲方应支付100万元"),
        ("服务费按10%支付。", "服务费按10支付"),
        ("单价为10.5万元。", "单价为105万元"),
        ("产品型号为ABC-10。", "产品型号为ABD10"),
    ],
)
async def test_window_extractor_preserves_business_significant_content(
    source: str,
    model_text: str,
) -> None:
    runtime = FakeRuntime(_model_output(model_text))

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _single_block_request(source),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_ALIGNMENT_FAILED"


@pytest.mark.asyncio
async def test_window_extractor_rejects_ambiguous_normalized_location() -> None:
    source = "乙 方应付款。\n乙方应 付款！"
    runtime = FakeRuntime(_model_output("乙方应付款"))

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _single_block_request(source),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "ALIGNMENT_AMBIGUOUS"


@pytest.mark.asyncio
async def test_window_extractor_rejects_unknown_schema_fields() -> None:
    payload = json.loads(_model_output())
    payload["extractions"][0]["confidence"] = 0.99
    runtime = FakeRuntime(json.dumps(payload, ensure_ascii=False))

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _request(),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_SCHEMA_INVALID"


@pytest.mark.asyncio
async def test_window_extractor_aligns_repeated_text_in_model_order() -> None:
    repeated = "乙方应交付成果。\n\n乙方应交付成果。"
    second_start = len("乙方应交付成果。") + 2
    request = WindowExtractionRequest(
        window_id="window-repeat",
        source_text=repeated,
        offset_map=[
            {
                "rendered_start": 0,
                "rendered_end": len("乙方应交付成果。"),
                "block_id": "block-001",
                "block_no": 1,
                "block_char_start": 0,
                "block_char_end": len("乙方应交付成果。"),
            },
            {
                "rendered_start": second_start,
                "rendered_end": len(repeated),
                "block_id": "block-002",
                "block_no": 2,
                "block_char_start": 0,
                "block_char_end": len("乙方应交付成果。"),
            },
        ],
    )
    item = json.loads(_model_output("乙方应交付成果"))["extractions"][0]
    runtime = FakeRuntime(json.dumps({"extractions": [item, item]}, ensure_ascii=False))

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        request,
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert [item.source_spans[0].block_id for item in result.extractions] == [
        "block-001",
        "block-002",
    ]


@pytest.mark.asyncio
async def test_window_extractor_allows_multiple_ir_types_on_same_source_span() -> None:
    text = "甲方应在验收后支付费用"
    common = {
        "extraction_text": text,
        "subject": "甲方",
        "predicate": "应支付",
        "object": "费用",
        "term": None,
        "meaning": None,
        "referenced_clause_nos": ["第二条"],
    }
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    {"extraction_class": "OBLIGATION", **common},
                    {"extraction_class": "PAYMENT", **common},
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _request(),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert [item.extraction_class for item in result.extractions] == [
        "OBLIGATION",
        "PAYMENT",
    ]
    assert result.extractions[0].rendered_char_start == result.extractions[1].rendered_char_start
    assert result.extractions[0].rendered_char_end == result.extractions[1].rendered_char_end
    assert all(item.source_spans[0].block_id == "block-002" for item in result.extractions)


def test_window_extractor_test_api_uses_configured_model(monkeypatch) -> None:
    monkeypatch.setenv("CONTRACT_MODEL_ID", "contract-model")
    monkeypatch.setenv("CONTRACT_TEST_TENANT_ID", "tenant-001")
    engine = FakeEngine()

    with TestClient(create_app(engine)) as client:
        response = client.post(
            "/api/extract-window",
            json={"window": _request().model_dump()},
        )

    assert response.status_code == 200
    assert response.json()["result"]["window_id"] == "window-001"
    assert engine.calls[0]["tenant_id"] == "tenant-001"
    assert engine.calls[0]["model_id"] == "contract-model"


def test_window_pipeline_test_api_returns_typed_merged_result(monkeypatch) -> None:
    monkeypatch.setenv("CONTRACT_MODEL_ID", "contract-model")
    monkeypatch.setenv("CONTRACT_TEST_TENANT_ID", "tenant-001")
    engine = FakeEngine()
    text = "合同标题"
    pipeline = {
        "document_id": "document-001",
        "generation_id": "generation-001",
        "expected_blocks": [{"block_id": "block-001", "text_length": len(text)}],
        "expected_section_ids": ["section-001"],
        "windows": [
            {
                "window_id": "window-001",
                "sequence_no": 1,
                "section_ids": ["section-001"],
                "heading_path": ["合同标题"],
                "clause_nos": [],
                "primary_block_ids": ["block-001"],
                "estimated_tokens": 4,
                "source_text": text,
                "context_text": "",
                "offset_map": [
                    {
                        "rendered_start": 0,
                        "rendered_end": len(text),
                        "block_id": "block-001",
                        "block_no": 1,
                        "block_char_start": 0,
                        "block_char_end": len(text),
                    }
                ],
            }
        ],
        "concurrency": 10,
    }

    with TestClient(create_app(engine)) as client:
        response = client.post("/api/extract-all", json={"pipeline": pipeline})

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["valid"] is True
    assert body["concurrency"] == 10
    assert body["semantic_ir"]["obligations"] == []
    assert engine.calls[0]["retry_feedback"] is None
