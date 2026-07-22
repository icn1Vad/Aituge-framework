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
            else ["OBLIGATION", "PAYMENT"]
        )
        return WindowExtractionResult(
            window_id=request.window_id,
            model_id=model_id,
            extractions=[
                _aligned(request, extraction_class=extraction_class)
                for extraction_class in classes
            ],
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
    assert [item for item in extractor.calls if item[0] == "window-002"] == [
        ("window-002", None),
        ("window-002", "WINDOW_OUTPUT_INVALID: first attempt failed"),
    ]
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
    assert extractor.calls == [
        ("window-001", None),
        ("window-001", "请删除主体简称定义，或扩展为唯一的完整定义句"),
    ]
    assert result.windows[0].attempts[0].error_message == "DEFINITION 存在多个候选位置"


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
async def test_source_grounded_item_id_ignores_model_predicate_wording_variation() -> None:
    request = _pipeline_request(count=1)
    first = await ContractIrWindowPipeline(
        extractor=FakePipelineExtractor(predicate="应履行")
    ).run(request, tenant_id="tenant-001", model_id="contract-model")
    second = await ContractIrWindowPipeline(
        extractor=FakePipelineExtractor(predicate="履行")
    ).run(request, tenant_id="tenant-001", model_id="contract-model")

    assert (
        first.semantic_ir.obligations[0].item_id
        == second.semantic_ir.obligations[0].item_id
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
async def test_pipeline_retries_suspicious_empty_then_fails_without_partial_ir() -> None:
    extractor = FakePipelineExtractor(empty=True)
    request = _pipeline_request(count=1, source="付款义务")

    with pytest.raises(WindowPipelineError) as exc_info:
        await ContractIrWindowPipeline(extractor=extractor).run(
            request,
            tenant_id="tenant-001",
            model_id="contract-model",
        )

    assert exc_info.value.code == "WINDOW_EXTRACTION_FAILED"
    assert extractor.call_counts == {"window-001": 2}
    details = exc_info.value.details
    assert details["coverage"]["valid"] is False
    assert details["windows"][0]["attempts"][0]["status"] == "SUSPICIOUS_EMPTY"
    assert details["windows"][0]["attempts"][1]["status"] == "SUSPICIOUS_EMPTY"


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
    assert len(result.semantic_ir.payment_terms) == 1


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
