import asyncio

import pytest

from services.contract.capabilities.register import ContractIrSemanticDelta
from services.contract.capabilities.window_extraction import (
    AlignedExtraction,
    SourceSpan,
    WindowExtractionError,
    WindowExtractionRequest,
    WindowExtractionResult,
    ValueCanonicalization,
)
from services.contract.capabilities.window_pipeline import (
    ContractIrWindowPipeline,
    WindowPipelineError,
    WindowPipelineRequest,
)


class FakePipelineExtractor:
    def __init__(
        self,
        *,
        fail_first: set[str] | None = None,
        empty: bool = False,
        predicate: str = "应履行",
        with_value_canonicalization: bool = False,
    ) -> None:
        self.fail_first = fail_first or set()
        self.empty = empty
        self.predicate = predicate
        self.with_value_canonicalization = with_value_canonicalization
        self.calls: list[tuple[str, str | None]] = []
        self.call_counts: dict[str, int] = {}
        self.active = 0
        self.max_active = 0

    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if (
                request.window_id in self.fail_first
                and self.call_counts[request.window_id] == 1
            ):
                raise WindowExtractionError("WINDOW_OUTPUT_INVALID", "first attempt failed")
            extractions = [] if self.empty else [_aligned(request, predicate=self.predicate)]
            return WindowExtractionResult(
                window_id=request.window_id,
                model_id=model_id,
                extractions=extractions,
                value_canonicalizations=(
                    [
                        ValueCanonicalization(
                            extraction_class="DATE",
                            value_family="TEMPORAL",
                            extraction_text="十五日内",
                            predicate="时间约束为",
                            binding_status="AMBIGUOUS",
                        )
                    ]
                    if self.with_value_canonicalization
                    else []
                ),
            )
        finally:
            self.active -= 1


class OneWindowAlwaysFailsExtractor(FakePipelineExtractor):
    def __init__(self, failed_window_id: str) -> None:
        super().__init__()
        self.failed_window_id = failed_window_id

    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        if request.window_id == self.failed_window_id:
            self.calls.append((request.window_id, retry_feedback))
            self.call_counts[request.window_id] = (
                self.call_counts.get(request.window_id, 0) + 1
            )
            raise WindowExtractionError(
                "WINDOW_OUTPUT_INVALID",
                "window remains invalid after retry",
            )
        return await super().extract(
            request,
            tenant_id=tenant_id,
            model_id=model_id,
            retry_feedback=retry_feedback,
        )


def _aligned(
    request: WindowExtractionRequest,
    *,
    predicate: str = "应履行",
    extraction_class: str = "OBLIGATION",
) -> AlignedExtraction:
    text = request.source_text
    return AlignedExtraction(
        extraction_class=extraction_class,
        extraction_text=text,
        subject="乙方",
        predicate=predicate,
        object=text,
        term=None,
        meaning=None,
        referenced_clause_nos=[],
        rendered_char_start=0,
        rendered_char_end=len(text),
        alignment_status="MATCH_EXACT",
        source_spans=[
            SourceSpan(
                block_id=request.offset_map[0].block_id,
                block_no=request.offset_map[0].block_no,
                block_char_start=0,
                block_char_end=len(text),
                quoted_text=text,
            )
        ],
    )


class CategoryRetryExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        classes = (
            ["OBLIGATION"]
            if self.call_counts[request.window_id] == 1
            else ["PAYMENT"]
        )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[
                _aligned(request, extraction_class=extraction_class)
                for extraction_class in classes
            ],
        )


class GoverningLawRetryExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        classes = (
            ["DISPUTE"]
            if self.call_counts[request.window_id] == 1
            else ["GOVERNING_LAW"]
        )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[
                _aligned(request, extraction_class=extraction_class)
                for extraction_class in classes
            ],
        )


class GoverningLawMissingExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[_aligned(request, extraction_class="DISPUTE")],
        )


class TargetedRetryExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        if self.call_counts[request.window_id] == 1:
            raise WindowExtractionError(
                "ALIGNMENT_AMBIGUOUS",
                "DEFINITION 存在多个候选位置",
                retry_feedback="请删除主体简称定义，或扩展为唯一的完整定义句",
            )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[_aligned(request)],
        )


class PartialAlignmentRetryExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        if self.call_counts[request.window_id] == 1:
            raise WindowExtractionError(
                "WINDOW_ALIGNMENT_FAILED",
                "RIGHT 未在当前 Window 原文中获得确定性字面匹配",
                retry_feedback="RIGHT 必须改为连续原文",
                accepted_extractions=[
                    _aligned(request, extraction_class="OBLIGATION")
                ],
            )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[_aligned(request, extraction_class="RIGHT")],
        )


class SameSpanSemanticRetryExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        if self.call_counts[request.window_id] == 1:
            raise WindowExtractionError(
                "WINDOW_ALIGNMENT_FAILED",
                "另一个语义项需要局部复查",
                retry_feedback="只补充另一个语义项",
                accepted_extractions=[
                    _aligned(request, predicate="应完成")
                ],
            )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[_aligned(request, predicate="应保证")],
        )


class AlignmentThenPaymentExtractor(FakePipelineExtractor):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult:
        self.calls.append((request.window_id, retry_feedback))
        self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
        if self.call_counts[request.window_id] == 1:
            raise WindowExtractionError(
                "ALIGNMENT_AMBIGUOUS",
                "DATE 存在多个候选位置",
                retry_feedback="DATE 必须扩展为唯一的完整条款",
            )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[
                _aligned(request, extraction_class="OBLIGATION"),
                _aligned(request, extraction_class="PAYMENT"),
                _aligned(request, extraction_class="DATE"),
            ],
        )


def _pipeline_request(count: int = 4, *, source: str = "履行事项") -> WindowPipelineRequest:
    expected_blocks = []
    expected_sections = []
    windows = []
    for index in range(1, count + 1):
        block_id = f"block-{index:03d}"
        section_id = f"section-{index:03d}"
        text = f"{source}{index}"
        expected_blocks.append({"block_id": block_id, "text_length": len(text)})
        expected_sections.append(section_id)
        windows.append(
            {
                "window_id": f"window-{index:03d}",
                "sequence_no": index,
                "section_ids": [section_id],
                "heading_path": [f"第{index}条"],
                "clause_nos": [f"第{index}条"],
                "primary_block_ids": [block_id],
                "estimated_tokens": 10,
                "source_text": text,
                "context_text": "",
                "offset_map": [
                    {
                        "rendered_start": 0,
                        "rendered_end": len(text),
                        "block_id": block_id,
                        "block_no": index,
                        "block_char_start": 0,
                        "block_char_end": len(text),
                        "page_number": index,
                    }
                ],
            }
        )
    return WindowPipelineRequest(
        document_id="document-001",
        generation_id="generation-001",
        expected_blocks=expected_blocks,
        expected_section_ids=expected_sections,
        windows=windows,
    )


def _single_window_request(
    source_text: str,
    *,
    heading_path: list[str],
    clause_nos: list[str],
) -> WindowPipelineRequest:
    request = _pipeline_request(count=1, source="placeholder")
    window = request.windows[0]
    window.source_text = source_text
    window.heading_path = heading_path
    window.clause_nos = clause_nos
    window.offset_map[0].rendered_end = len(source_text)
    window.offset_map[0].block_char_end = len(source_text)
    request.expected_blocks[0].text_length = len(source_text)
    return request


@pytest.mark.asyncio
async def test_pipeline_runs_rolling_concurrency_ten_and_retries_only_failed_window() -> None:
    extractor = FakePipelineExtractor(fail_first={"window-002"})
    pipeline = ContractIrWindowPipeline(extractor=extractor)

    result = await pipeline.run(
        _pipeline_request(count=12),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.max_active == 10
    assert pipeline.max_observed_concurrency == 10
    assert extractor.call_counts == {
        "window-001": 1,
        "window-002": 2,
        "window-003": 1,
        "window-004": 1,
        "window-005": 1,
        "window-006": 1,
        "window-007": 1,
        "window-008": 1,
        "window-009": 1,
        "window-010": 1,
        "window-011": 1,
        "window-012": 1,
    }
    window_two_calls = [item for item in extractor.calls if item[0] == "window-002"]
    assert window_two_calls[0] == ("window-002", None)
    assert "WINDOW_OUTPUT_INVALID: first attempt failed" in (window_two_calls[1][1] or "")
    assert "当前没有可保留的已验证项" in (window_two_calls[1][1] or "")
    assert result.model_call_count == 13
    assert result.retry_count == 1
    assert result.coverage.valid is True
    assert result.coverage.processed_window_count == 12
    assert [item.item_id for item in result.semantic_ir.obligations] == [
        item.contract_value.item_id
        for window in result.windows
        for item in window.mapped_extractions
    ]
    ContractIrSemanticDelta.model_validate(result.semantic_ir.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_pipeline_uses_private_targeted_feedback_for_local_retry() -> None:
    extractor = TargetedRetryExtractor()

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.coverage.valid is True
    assert extractor.calls[0] == ("window-001", None)
    assert "请删除主体简称定义，或扩展为唯一的完整定义句" in (
        extractor.calls[1][1] or ""
    )
    assert "当前没有可保留的已验证项" in (extractor.calls[1][1] or "")
    assert result.windows[0].attempts[0].error_message == "DEFINITION 存在多个候选位置"


@pytest.mark.asyncio
async def test_pipeline_preserves_valid_items_and_merges_only_retry_delta() -> None:
    extractor = PartialAlignmentRetryExtractor()

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.coverage.valid is True
    assert extractor.call_counts == {"window-001": 2}
    feedback = extractor.calls[1][1] or ""
    assert "RIGHT 必须改为连续原文" in feedback
    assert "已有 1 项通过严格原文校验并由系统保留" in feedback
    assert "只返回需要修复或补齐的增量项" in feedback
    assert len(result.semantic_ir.obligations) == 1
    assert len(result.semantic_ir.rights) == 1


@pytest.mark.asyncio
async def test_retry_merge_preserves_distinct_semantics_on_same_source_span() -> None:
    extractor = SameSpanSemanticRetryExtractor()

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.coverage.valid is True
    assert extractor.call_counts == {"window-001": 2}
    assert [item.predicate for item in result.semantic_ir.obligations] == [
        "应完成",
        "应保证",
    ]
    assert {
        tuple(
            (
                anchor.block_id,
                anchor.char_start,
                anchor.char_end,
            )
            for anchor in item.source_anchors
        )
        for item in result.semantic_ir.obligations
    } == {(("block-001", 0, len("履行事项1")),)}


@pytest.mark.asyncio
async def test_alignment_retry_also_requires_all_strong_categories() -> None:
    extractor = AlignmentThenPaymentExtractor()

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1, source="甲方逾期付款超过30个工作日"),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.coverage.valid is True
    assert extractor.call_counts == {"window-001": 2}
    feedback = extractor.calls[1][1] or ""
    assert "DATE 必须扩展为唯一的完整条款" in feedback
    assert "强指示类别：PAYMENT、DATE" in feedback
    assert len(result.semantic_ir.payment_terms) == 1
    assert len(result.semantic_ir.dates) == 1


@pytest.mark.asyncio
async def test_pipeline_requires_explicit_amount_category() -> None:
    class AmountRetryExtractor(FakePipelineExtractor):
        async def extract(
            self,
            request: WindowExtractionRequest,
            *,
            tenant_id: str,
            model_id: str,
            retry_feedback: str | None = None,
        ) -> WindowExtractionResult:
            self.calls.append((request.window_id, retry_feedback))
            self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
            classes = (
                ["OBLIGATION"]
                if self.call_counts[request.window_id] == 1
                else ["OBLIGATION", "AMOUNT"]
            )
            return WindowExtractionResult(
                window_id=request.window_id,
                model_id=model_id,
                extractions=[
                    _aligned(request, extraction_class=extraction_class)
                    for extraction_class in classes
                ],
            )

    extractor = AmountRetryExtractor()
    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1, source="乙方造成损失超过30000元"),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2}
    assert "AMOUNT 类别指示" in (extractor.calls[1][1] or "")
    assert len(result.semantic_ir.amounts) == 1


@pytest.mark.asyncio
async def test_pipeline_is_deterministic_for_same_input() -> None:
    request = _pipeline_request(count=2)
    first = await ContractIrWindowPipeline(extractor=FakePipelineExtractor()).run(
        request,
        tenant_id="tenant-001",
        model_id="contract-model",
    )
    second = await ContractIrWindowPipeline(extractor=FakePipelineExtractor()).run(
        request,
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert first.semantic_ir_hash == second.semantic_ir_hash
    assert first.semantic_ir == second.semantic_ir


@pytest.mark.asyncio
async def test_item_id_distinguishes_semantic_variants_on_the_same_source() -> None:
    request = _pipeline_request(count=1)
    first = await ContractIrWindowPipeline(
        extractor=FakePipelineExtractor(predicate="应履行")
    ).run(request, tenant_id="tenant-001", model_id="contract-model")
    second = await ContractIrWindowPipeline(
        extractor=FakePipelineExtractor(predicate="履行")
    ).run(request, tenant_id="tenant-001", model_id="contract-model")

    assert (
        first.semantic_ir.obligations[0].item_id
        != second.semantic_ir.obligations[0].item_id
    )
    assert first.semantic_ir_hash != second.semantic_ir_hash


@pytest.mark.asyncio
async def test_pipeline_rejects_incomplete_primary_block_coverage_before_model_call() -> None:
    request = _pipeline_request(count=1)
    request.windows[0].offset_map[0].block_char_end -= 1
    request.windows[0].offset_map[0].rendered_end -= 1
    request.windows[0].source_text = request.windows[0].source_text[:-1]
    extractor = FakePipelineExtractor()

    with pytest.raises(WindowPipelineError) as exc_info:
        await ContractIrWindowPipeline(extractor=extractor).run(
            request,
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_COVERAGE_INVALID"
    assert extractor.calls == []


@pytest.mark.asyncio
async def test_pipeline_recovers_suspicious_empty_window_from_exact_source() -> None:
    extractor = FakePipelineExtractor(empty=True)
    request = _pipeline_request(count=1, source="付款义务")

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        request,
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2}
    assert result.coverage.valid is True
    assert result.windows[0].attempts[0].status == "SUSPICIOUS_EMPTY"
    assert result.windows[0].attempts[1].status == "SUCCEEDED"
    assert result.windows[0].attempts[1].fallback_extraction_count >= 1
    assert len(result.semantic_ir.payment_terms) == 1
    assert result.semantic_ir.payment_terms[0].predicate == "原文待模型复核"


@pytest.mark.asyncio
async def test_pipeline_recovers_alignment_failure_from_exact_source() -> None:
    class AlignmentFailureExtractor(FakePipelineExtractor):
        async def extract(
            self,
            request: WindowExtractionRequest,
            *,
            tenant_id: str,
            model_id: str,
            retry_feedback: str | None = None,
        ) -> WindowExtractionResult:
            self.calls.append((request.window_id, retry_feedback))
            self.call_counts[request.window_id] = self.call_counts.get(request.window_id, 0) + 1
            raise WindowExtractionError(
                "WINDOW_ALIGNMENT_FAILED",
                "OBLIGATION did not match an exact source span",
            )

    extractor = AlignmentFailureExtractor()
    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1, source="乙方应履行保密义务"),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2}
    assert result.coverage.valid is True
    assert result.windows[0].attempts[1].status == "SUCCEEDED"
    assert result.windows[0].attempts[1].fallback_extraction_count >= 1
    assert result.semantic_ir.obligations[0].predicate == "原文待模型复核"


@pytest.mark.asyncio
async def test_pipeline_accepts_reviewed_empty_non_business_window() -> None:
    extractor = FakePipelineExtractor(empty=True)
    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1, source="合同标题"),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert result.retry_count == 0
    assert result.semantic_ir.obligations == []
    assert result.coverage.valid is True


@pytest.mark.asyncio
async def test_pipeline_keeps_other_ir_when_exactly_one_window_fails() -> None:
    extractor = OneWindowAlwaysFailsExtractor("window-001")

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=2),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2, "window-002": 1}
    assert result.coverage.valid is False
    assert result.coverage.processed_window_count == 1
    assert result.coverage.failed_window_ids == ["window-001"]
    assert result.retry_count == 1
    assert result.model_call_count == 3
    assert [item.status for item in result.windows] == ["FAILED", "SUCCEEDED"]
    assert len(result.semantic_ir.obligations) == 1
    assert result.semantic_ir.obligations[0].object == "履行事项2"


@pytest.mark.asyncio
async def test_pipeline_ignores_document_title_cues_before_current_clause_body() -> None:
    source_text = "\n\n".join(
        (
            "\u5e76\u53d1\u6d4b\u8bd5\u6837\u672c C03-R2 | \u4ed8\u6b3e\u9a8c\u6536\u98ce\u9669",
            "\u5408\u540c\u91d1\u989d/\u79df\u91d1 | \u6bcf\u6708\u79df\u91d1\u4eba\u6c11\u5e01 186,000 \u5143\uff08\u542b\u7a0e\uff09",
            "\u7b2c\u4e00\u6761 \u79df\u8d41\u6807\u7684",
            "1.1 \u79df\u8d41\u9762\u79ef\u4e3a\u5efa\u7b51\u9762\u79ef 2,100 \u5e73\u65b9\u7c73\u3002",
            "1.2 \u7532\u65b9\u4fdd\u8bc1\u5bf9\u79df\u8d41\u623f\u5c4b\u62e5\u6709\u5408\u6cd5\u51fa\u79df\u6743\u3002",
        )
    )
    extractor = FakePipelineExtractor()

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _single_window_request(
            source_text,
            heading_path=["\u7b2c\u4e00\u6761 \u79df\u8d41\u6807\u7684"],
            clause_nos=["\u7b2c\u4e00\u6761"],
        ),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 1}
    assert result.retry_count == 0
    assert result.coverage.valid is True


@pytest.mark.asyncio
async def test_pipeline_allows_empty_signature_only_window() -> None:
    source_text = "\n\n".join(
        (
            "\u7b7e\u7f72\u9875",
            "\u7532\u65b9\uff1a\u661f\u6cb3\u667a\u9020\u6709\u9650\u516c\u53f8 | \u4e59\u65b9\uff1a\u4e91\u5c9a\u6570\u79d1\u6709\u9650\u516c\u53f8",
            "\u6388\u6743\u4ee3\u8868\uff1a____________ | \u6388\u6743\u4ee3\u8868\uff1a____________",
            "\u7b7e\u7f72\u65e5\u671f\uff1a____\u5e74__\u6708__\u65e5 | \u7b7e\u7f72\u65e5\u671f\uff1a____\u5e74__\u6708__\u65e5",
            "\uff08\u76d6\u7ae0\uff09 | \uff08\u76d6\u7ae0\uff09",
        )
    )
    extractor = FakePipelineExtractor(empty=True)

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _single_window_request(
            source_text,
            heading_path=["\u7b7e\u7f72\u9875"],
            clause_nos=[],
        ),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 1}
    assert result.retry_count == 0
    assert result.coverage.valid is True


@pytest.mark.asyncio
async def test_pipeline_keeps_real_acceptance_clause_strict() -> None:
    extractor = FakePipelineExtractor()

    with pytest.raises(WindowPipelineError) as exc_info:
        await ContractIrWindowPipeline(extractor=extractor).run(
            _single_window_request(
                "\u7b2c\u516d\u6761 \u9a8c\u6536\n\u7532\u65b9\u5e94\u5f53\u5b8c\u6210\u9a8c\u6536\u3002",
                heading_path=["\u7b2c\u516d\u6761 \u9a8c\u6536"],
                clause_nos=["\u7b2c\u516d\u6761"],
            ),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_EXTRACTION_FAILED"
    assert extractor.call_counts == {"window-001": 2}
    attempts = exc_info.value.details["windows"][0]["attempts"]
    assert attempts[0]["status"] == "SUSPICIOUS_CATEGORY"
    assert "ACCEPTANCE" in attempts[0]["error_message"]


@pytest.mark.asyncio
async def test_pipeline_keeps_substantive_signature_window_strict() -> None:
    extractor = FakePipelineExtractor(empty=True)

    with pytest.raises(WindowPipelineError) as exc_info:
        await ContractIrWindowPipeline(extractor=extractor).run(
            _single_window_request(
                "\u7b7e\u7f72\u9875\n\u7532\u65b9\uff1a\u672c\u5408\u540c\u7b7e\u7f72\u540e\u5e94\u652f\u4ed8\u670d\u52a1\u8d39\u3002",
                heading_path=["\u7b7e\u7f72\u9875"],
                clause_nos=[],
            ),
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_EXTRACTION_FAILED"
    assert extractor.call_counts == {"window-001": 2}
    attempt = exc_info.value.details["windows"][0]["attempts"][0]
    assert attempt["status"] == "SUSPICIOUS_CATEGORY"
    assert "PAYMENT" in attempt["error_message"]


@pytest.mark.asyncio
async def test_pipeline_retries_strong_category_cue_missing_from_first_result() -> None:
    extractor = CategoryRetryExtractor()

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1, source="乙方应在付款前提供发票"),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2}
    assert result.retry_count == 1
    assert result.windows[0].attempts[0].status == "SUSPICIOUS_CATEGORY"
    assert result.windows[0].attempts[0].error_code == "WINDOW_CATEGORY_MISSING"
    assert "PAYMENT" in (extractor.calls[1][1] or "")
    assert "只返回缺少类别的增量项" in (extractor.calls[1][1] or "")
    assert len(result.semantic_ir.obligations) == 1
    assert len(result.semantic_ir.payment_terms) == 1


@pytest.mark.asyncio
async def test_pipeline_ignores_category_cue_from_stale_heading_not_in_window_source() -> None:
    extractor = FakePipelineExtractor()
    source = "第七条 未经合作方同意，不得将研究开发工作转让。"
    stale_heading = "第四条 甲方按如下方式提供或支付研究开发经费及其他投资"

    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _single_window_request(
            source,
            heading_path=[stale_heading],
            clause_nos=["第七条"],
        ),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 1}
    assert result.retry_count == 0
    assert result.coverage.valid is True
    assert result.semantic_ir.payment_terms == []


@pytest.mark.asyncio
async def test_pipeline_reports_value_canonicalization_without_retrying_window() -> None:
    extractor = FakePipelineExtractor(with_value_canonicalization=True)
    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _pipeline_request(count=1),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 1}
    assert result.model_call_count == 1
    assert result.retry_count == 0
    attempt = result.windows[0].attempts[0]
    assert attempt.value_canonicalization_count == 1
    assert attempt.ambiguous_value_count == 1
    assert attempt.unbound_value_count == 0


@pytest.mark.asyncio
async def test_pipeline_requires_and_maps_governing_law_separately_from_dispute_path() -> None:
    extractor = GoverningLawRetryExtractor()
    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _single_window_request(
            "\u7b2c\u5341\u6761 \u4e89\u8bae\u89e3\u51b3\n"
            "10.1 \u672c\u5408\u540c\u9002\u7528\u4e2d\u534e\u4eba\u6c11\u5171\u548c\u56fd\u6cd5\u5f8b\u3002\n"
            "10.2 \u534f\u5546\u4e0d\u6210\u7684\uff0c\u4efb\u4f55\u4e00\u65b9\u53ef\u5411\u6709\u7ba1\u8f96\u6743\u7684\u4eba\u6c11\u6cd5\u9662\u8d77\u8bc9\u3002",
            heading_path=["\u7b2c\u5341\u6761 \u4e89\u8bae\u89e3\u51b3"],
            clause_nos=["\u7b2c\u5341\u6761"],
        ),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2}
    assert "GOVERNING_LAW" in (extractor.calls[1][1] or "")
    assert len(result.semantic_ir.dispute_resolution) == 2


@pytest.mark.asyncio
async def test_pipeline_recovers_only_missing_governing_law_from_exact_source() -> None:
    extractor = GoverningLawMissingExtractor()
    result = await ContractIrWindowPipeline(extractor=extractor).run(
        _single_window_request(
            "第十条 争议解决\n"
            "10.1 本合同适用中华人民共和国法律。\n"
            "10.2 协商不成的，任何一方可向有管辖权的人民法院起诉。",
            heading_path=["第十条 争议解决"],
            clause_nos=["第十条"],
        ),
        tenant_id="tenant-001",
        model_id="contract-model",
    )

    assert extractor.call_counts == {"window-001": 2}
    assert result.windows[0].attempts[1].fallback_extraction_count == 1
    assert len(result.semantic_ir.dispute_resolution) == 2
