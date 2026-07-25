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


def _party_context() -> dict[str, str]:
    return {
        "party_a_name": "甲方测试单位",
        "party_b_name": "乙方测试单位",
        "perspective": "PARTY_A",
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
    }


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


def _semantic_item(
    extraction_class: str,
    extraction_text: str,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    object_: str | None = None,
) -> dict:
    return {
        "extraction_class": extraction_class,
        "extraction_text": extraction_text,
        "subject": subject,
        "predicate": predicate,
        "object": object_,
        "term": None,
        "meaning": None,
        "referenced_clause_nos": [],
    }


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
    assert "发票税费、调价、扣款抵销" in call["system_prompt"]
    assert "一般服务质量、响应时限" in call["system_prompt"]
    assert "仅引用适用法律不等于争议解决" in call["system_prompt"]
    assert "context_only 中的合同主体" in call["system_prompt"]
    assert "不得把合同当事人" in call["system_prompt"]
    assert "同时包含 term 和 meaning 的完整定义性原文句段" in call["system_prompt"]
    assert "不得只返回重复出现的术语短词" in call["system_prompt"]

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

    obligation = next(
        item for item in result.extractions if item.extraction_class == "OBLIGATION"
    )
    assert obligation.source_spans[0].block_id == "block-001"


@pytest.mark.asyncio
async def test_window_extractor_rejects_ungrounded_or_paraphrased_text() -> None:
    runtime = FakeRuntime(_model_output("乙方需要尽快交付成果"))

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _request(),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    error = exc_info.value
    assert error.code == "WINDOW_ALIGNMENT_FAILED"
    assert str(error) == "OBLIGATION 未在当前 Window 原文中获得确定性字面匹配"
    assert "OBLIGATION extraction_text=\"乙方需要尽快交付成果\"" in (
        error.retry_feedback or ""
    )
    assert "如果原文没有对应依据就删除该项" in (error.retry_feedback or "")
    assert "乙方需要尽快交付成果" not in str(error)
    assert [item.extraction_class for item in error.accepted_extractions] == ["DATE"]
    assert error.accepted_extractions[0].extraction_text == "十日"


@pytest.mark.asyncio
async def test_window_extractor_reports_all_unaligned_items_in_private_feedback() -> None:
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    _semantic_item(
                        "OBLIGATION",
                        "乙方需要尽快交付成果",
                        subject="乙方",
                        predicate="需要交付",
                        object_="成果",
                    ),
                    _semantic_item(
                        "PAYMENT",
                        "甲方随后把费用付清",
                        subject="甲方",
                        predicate="支付",
                        object_="费用",
                    ),
                ]
            },
            ensure_ascii=False,
        )
    )

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _request(),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    error = exc_info.value
    assert error.code == "WINDOW_ALIGNMENT_FAILED"
    assert str(error) == "2 条抽取项未在当前 Window 原文中获得确定性字面匹配"
    assert "1. OBLIGATION extraction_text=\"乙方需要尽快交付成果\"" in (
        error.retry_feedback or ""
    )
    assert "2. PAYMENT extraction_text=\"甲方随后把费用付清\"" in (
        error.retry_feedback or ""
    )
    assert "禁止摘要、改写、补字或拼接不连续句段" in (error.retry_feedback or "")
    assert "乙方需要尽快交付成果" not in str(error)
    assert "甲方随后把费用付清" not in str(error)


@pytest.mark.asyncio
async def test_window_extractor_keeps_valid_items_when_another_item_is_unaligned() -> None:
    source = "乙方应在十日内交付成果。甲方有权要求整改。"
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    _semantic_item(
                        "OBLIGATION",
                        "乙方应在十日内交付成果",
                        subject="乙方",
                        predicate="应交付",
                        object_="成果",
                    ),
                    _semantic_item(
                        "RIGHT",
                        "甲方可以要求乙方立即完成整改",
                        subject="甲方",
                        predicate="有权要求",
                        object_="整改",
                    ),
                ]
            },
            ensure_ascii=False,
        )
    )

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _single_block_request(source),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    accepted = exc_info.value.accepted_extractions
    assert [item.extraction_class for item in accepted] == ["OBLIGATION", "DATE"]
    assert accepted[0].extraction_text == "乙方应在十日内交付成果"
    assert accepted[1].extraction_text == "十日"


@pytest.mark.asyncio
async def test_window_extractor_adds_explicit_date_and_amount_from_source() -> None:
    source = (
        "甲方可要求乙方承担每人次200至2000元的违约金。"
        "甲方逾期付款的，每逾期一天，应继续支付违约金。"
    )
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    _semantic_item(
                        "RIGHT",
                        "甲方可要求乙方承担每人次200至2000元的违约金",
                        subject="甲方",
                        predicate="可要求",
                        object_="乙方承担违约金",
                    )
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    values = {
        (item.extraction_class, item.extraction_text): item
        for item in result.extractions
        if item.extraction_class in {"DATE", "AMOUNT"}
    }
    assert set(values) == {("AMOUNT", "2000元"), ("DATE", "一天")}
    assert values[("AMOUNT", "2000元")].predicate == "数值约束为"
    assert values[("DATE", "一天")].predicate == "时间约束为"
    assert all(item.source_spans[0].quoted_text == item.extraction_text for item in values.values())


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
async def test_definition_ambiguity_produces_private_targeted_retry_feedback() -> None:
    source = "服务标准是指附件约定的质量要求。服务标准适用于全部交付物。"
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    {
                        "extraction_class": "DEFINITION",
                        "extraction_text": "服务标准",
                        "subject": None,
                        "predicate": None,
                        "object": None,
                        "term": "服务标准",
                        "meaning": "附件约定的质量要求",
                        "referenced_clause_nos": [],
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _single_block_request(source),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    error = exc_info.value
    assert error.code == "ALIGNMENT_AMBIGUOUS"
    assert str(error) == "DEFINITION 规范化后在当前 Window 原文中存在 2 个候选位置"
    assert "extraction_text=\"服务标准\"" in (error.retry_feedback or "")
    assert "请删除该 DEFINITION" in (error.retry_feedback or "")
    assert "完整连续定义性原文句段" in (error.retry_feedback or "")
    assert "服务标准" not in str(error)


@pytest.mark.asyncio
async def test_window_extractor_drops_party_and_document_alias_definitions() -> None:
    source = "双方签订本合同（以下简称“本合同”）。甲方应支付服务费用。"
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    {
                        "extraction_class": "DEFINITION",
                        "extraction_text": "双方签订本合同（以下简称“本合同”）",
                        "subject": None,
                        "predicate": None,
                        "object": None,
                        "term": "本合同",
                        "meaning": "双方签订的合同",
                        "referenced_clause_nos": [],
                    },
                    {
                        "extraction_class": "PAYMENT",
                        "extraction_text": "甲方应支付服务费用",
                        "subject": "甲方",
                        "predicate": "应支付",
                        "object": "服务费用",
                        "term": None,
                        "meaning": None,
                        "referenced_clause_nos": [],
                    },
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert [item.extraction_class for item in result.extractions] == ["PAYMENT"]


@pytest.mark.asyncio
async def test_date_full_clause_preserves_model_value_during_canonicalization() -> None:
    source = "甲方逾期付款超过30个工作日时，乙方有权解除合同。"
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    {
                        "extraction_class": "DATE",
                        "extraction_text": source,
                        "subject": "甲方逾期付款",
                        "predicate": None,
                        "object": "30个工作日",
                        "term": None,
                        "meaning": None,
                        "referenced_clause_nos": [],
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.extractions[0].predicate == "时间约束为"
    assert result.extractions[0].object == "30个工作日"
    assert result.extractions[0].extraction_text == source


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

    shared_span_items = [
        item
        for item in result.extractions
        if item.extraction_class in {"OBLIGATION", "PAYMENT"}
    ]
    assert [item.extraction_class for item in shared_span_items] == [
        "OBLIGATION",
        "PAYMENT",
    ]
    assert shared_span_items[0].rendered_char_start == shared_span_items[1].rendered_char_start
    assert shared_span_items[0].rendered_char_end == shared_span_items[1].rendered_char_end
    assert all(item.source_spans[0].block_id == "block-002" for item in shared_span_items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extraction_class", "source", "value", "expected_predicate", "family"),
    [
        ("DATE", "乙方应在收到材料后十五个工作日内完成交付。", "十五个工作日内", "时间约束为", "TEMPORAL"),
        ("DATE", "服务期限自生效日起连续三十六个月。", "三十六个月", "时间约束为", "TEMPORAL"),
        ("AMOUNT", "违约方应按合同总价的百分之十二支付违约金。", "百分之十二", "数值约束为", "NUMERIC"),
        ("AMOUNT", "每次服务的费用区间为人民币800至1500元。", "人民币800至1500元", "数值约束为", "NUMERIC"),
    ],
)
async def test_window_extractor_canonicalizes_arbitrary_grounded_values_without_literal_rules(
    extraction_class: str,
    source: str,
    value: str,
    expected_predicate: str,
    family: str,
) -> None:
    parent = _semantic_item(
        "OBLIGATION",
        source[:-1],
        subject="合同当事方",
        predicate="应履行",
        object_="约定事项",
    )
    raw_value = _semantic_item(extraction_class, value)
    runtime = FakeRuntime(
        json.dumps({"extractions": [parent, raw_value]}, ensure_ascii=False)
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    value_item = next(
        item for item in result.extractions if item.extraction_class == extraction_class
    )
    assert value_item.subject == "约定事项"
    assert value_item.predicate == expected_predicate
    assert value_item.object == value
    assert value_item.extraction_text == value
    assert value_item.source_spans[0].quoted_text == value
    assert len(result.value_canonicalizations) == 1
    diagnostic = result.value_canonicalizations[0]
    assert diagnostic.value_family == family
    assert diagnostic.binding_status == "BOUND_CONTAINING"
    assert diagnostic.related_extraction_class == "OBLIGATION"


@pytest.mark.asyncio
async def test_window_extractor_preserves_model_supplied_value_relationship() -> None:
    source = "履约保证金为合同金额的8%。"
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    _semantic_item(
                        "AMOUNT",
                        "合同金额的8%",
                        subject="履约保证金",
                        predicate="计取标准为",
                        object_="合同金额的8%",
                    )
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.extractions[0].predicate == "计取标准为"
    assert result.extractions[0].object == "合同金额的8%"
    assert result.value_canonicalizations == []


@pytest.mark.asyncio
async def test_window_extractor_keeps_ambiguous_value_grounded_without_guessing_relation() -> None:
    source = "甲方应在二十日内支付全部服务费。"
    clause = source[:-1]
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    _semantic_item(
                        "OBLIGATION",
                        clause,
                        subject="甲方",
                        predicate="应履行",
                        object_="付款义务",
                    ),
                    _semantic_item(
                        "PAYMENT",
                        clause,
                        subject="甲方",
                        predicate="应支付",
                        object_="服务费",
                    ),
                    _semantic_item("DATE", "二十日内"),
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    value_item = next(item for item in result.extractions if item.extraction_class == "DATE")
    assert value_item.subject is None
    assert value_item.predicate == "时间约束为"
    assert value_item.object == "二十日内"
    assert result.value_canonicalizations[0].binding_status == "AMBIGUOUS"
    assert result.value_canonicalizations[0].related_extraction_class is None


@pytest.mark.asyncio
async def test_window_extractor_binds_value_to_only_semantic_item_in_same_sentence() -> None:
    source = "甲方应支付服务费，付款期限为四十五日内。"
    runtime = FakeRuntime(
        json.dumps(
            {
                "extractions": [
                    _semantic_item(
                        "PAYMENT",
                        "甲方应支付服务费",
                        subject="甲方",
                        predicate="应支付",
                        object_="服务费",
                    ),
                    _semantic_item("DATE", "四十五日内"),
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    value_item = next(item for item in result.extractions if item.extraction_class == "DATE")
    assert value_item.subject == "服务费"
    assert result.value_canonicalizations[0].binding_status == "BOUND_SENTENCE"
    assert result.value_canonicalizations[0].related_extraction_class == "PAYMENT"


@pytest.mark.asyncio
async def test_window_extractor_keeps_standalone_value_without_inventing_subject() -> None:
    source = "合同暂定总价人民币235000元。"
    runtime = FakeRuntime(
        json.dumps(
            {"extractions": [_semantic_item("AMOUNT", "人民币235000元")]},
            ensure_ascii=False,
        )
    )

    result = await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
        _single_block_request(source),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    value_item = result.extractions[0]
    assert value_item.subject is None
    assert value_item.predicate == "数值约束为"
    assert value_item.object == "人民币235000元"
    assert result.value_canonicalizations[0].binding_status == "UNBOUND"


@pytest.mark.asyncio
async def test_window_extractor_still_rejects_missing_predicate_for_non_value_class() -> None:
    runtime = FakeRuntime(
        json.dumps(
            {"extractions": [_semantic_item("PAYMENT", "甲方支付服务费")]},
            ensure_ascii=False,
        )
    )

    with pytest.raises(WindowExtractionError) as exc_info:
        await WindowExtractionEngine(runtime_factory=lambda _: runtime).extract(
            _single_block_request("甲方支付服务费。"),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_SCHEMA_INVALID"


def test_window_extractor_test_api_uses_configured_model(monkeypatch) -> None:
    monkeypatch.setenv("CONTRACT_MODEL_ID", "contract-model")
    monkeypatch.setenv("CONTRACT_TEST_TENANT_ID", "tenant-001")
    engine = FakeEngine()

    with TestClient(create_app(engine)) as client:
        response = client.post(
            "/api/extract-window",
            json={
                "window": _request().model_dump(),
                "party_context": _party_context(),
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["window_id"] == "window-001"
    assert engine.calls[0]["tenant_id"] == "tenant-001"
    assert engine.calls[0]["model_id"] == "contract-model"
    assert response.json()["party_context"]["our_party"] == "甲方测试单位"
    assert response.json()["party_context"]["counterparty"] == "乙方测试单位"
    sent_window = engine.calls[0]["window"]
    assert "PARTY_A_NAME=甲方测试单位" in sent_window.context_text
    assert "OUR_PARTY=甲方测试单位" in sent_window.context_text
    assert "甲方测试单位" not in sent_window.source_text
    assert sent_window.offset_map == _request().offset_map


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
        response = client.post(
            "/api/extract-all",
            json={"pipeline": pipeline, "party_context": _party_context()},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["valid"] is True
    assert body["concurrency"] == 10
    assert body["semantic_ir"]["obligations"] == []
    assert body["party_context"]["perspective"] == "PARTY_A"
    assert body["party_context"]["our_party"] == "甲方测试单位"
    assert engine.calls[0]["retry_feedback"] is None
    assert "COUNTERPARTY=乙方测试单位" in engine.calls[0]["window"].context_text
    assert engine.calls[0]["window"].source_text == text


def test_window_extractor_test_api_rejects_identical_parties(monkeypatch) -> None:
    monkeypatch.setenv("CONTRACT_MODEL_ID", "contract-model")
    context = _party_context()
    context["party_b_name"] = context["party_a_name"]

    with TestClient(create_app(FakeEngine())) as client:
        response = client.post(
            "/api/extract-window",
            json={"window": _request().model_dump(), "party_context": context},
        )

    assert response.status_code == 422
