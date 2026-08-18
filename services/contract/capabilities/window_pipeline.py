"""Deterministic orchestration for Window-based Contract IR extraction.

This module is intentionally internal to the Contract capability.  It does not
change the public Java/Python protocol or the external ``extract_contract_ir``
stage shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

try:
    from services.contract.capabilities.window_extraction import (
        AlignedExtraction,
        EXPLICIT_AMOUNT_PATTERN,
        EXPLICIT_DATE_PATTERN,
        SourceSpan,
        ValueCanonicalization,
        WindowExtractionRequest,
        WindowExtractionResult,
    )
except ModuleNotFoundError as exc:  # standalone capability mount in the runtime image
    if exc.name != "services":
        raise
    from window_extraction import (
        AlignedExtraction,
        EXPLICIT_AMOUNT_PATTERN,
        EXPLICIT_DATE_PATTERN,
        SourceSpan,
        ValueCanonicalization,
        WindowExtractionRequest,
        WindowExtractionResult,
    )


IR_FIELD_BY_CLASS = {
    "DEFINITION": "definitions",
    "RIGHT": "rights",
    "OBLIGATION": "obligations",
    "PROHIBITION": "prohibitions",
    "PAYMENT": "payment_terms",
    "DELIVERY": "delivery_terms",
    "ACCEPTANCE": "acceptance_terms",
    "LIABILITY": "liabilities",
    "TERMINATION": "termination_terms",
    "CONFIDENTIALITY": "confidentiality_terms",
    "INTELLECTUAL_PROPERTY": "intellectual_property_terms",
    "GOVERNING_LAW": "dispute_resolution",
    "DISPUTE": "dispute_resolution",
    "DATE": "dates",
    "AMOUNT": "amounts",
}
IR_FIELDS = tuple(IR_FIELD_BY_CLASS.values())
WINDOW_EXTRACTION_CONCURRENCY = 10
_CRITICAL_CONTENT = re.compile(
    r"付款|费用|价款|金额|交付|验收|违约|赔偿|责任|解除|终止|保密|"
    r"知识产权|争议|仲裁|诉讼|权利|义务|应当|必须|不得|日期|期限"
)
_CATEGORY_CUES = {
    "PAYMENT": re.compile(r"付款|支付|价款|费用|结算|发票|税费|扣款|抵扣|抵销|冲抵"),
    "ACCEPTANCE": re.compile(r"验收"),
    "GOVERNING_LAW": re.compile(r"(?:适用|依据).{0,12}(?:中华人民共和国)?法律"),
    "DISPUTE": re.compile(r"争议|仲裁|诉讼|管辖|人民法院"),
    "DATE": EXPLICIT_DATE_PATTERN,
    "AMOUNT": EXPLICIT_AMOUNT_PATTERN,
}
_FALLBACK_CLASS_CUES = {
    "GOVERNING_LAW": _CATEGORY_CUES["GOVERNING_LAW"],
    "PAYMENT": _CATEGORY_CUES["PAYMENT"],
    "ACCEPTANCE": _CATEGORY_CUES["ACCEPTANCE"],
    "DISPUTE": _CATEGORY_CUES["DISPUTE"],
    "DELIVERY": re.compile(r"交付|交货|移交|交付物"),
    "LIABILITY": re.compile(r"违约|赔偿|责任|损失|违约金"),
    "TERMINATION": re.compile(r"解除|终止"),
    "CONFIDENTIALITY": re.compile(r"保密|秘密信息"),
    "INTELLECTUAL_PROPERTY": re.compile(r"知识产权|专利|著作权|商标"),
    "PROHIBITION": re.compile(r"不得|禁止"),
    "RIGHT": re.compile(r"有权|可以|可"),
    "OBLIGATION": re.compile(r"应当|应|须|必须|负责|承担|保证"),
    "DATE": EXPLICIT_DATE_PATTERN,
    "AMOUNT": EXPLICIT_AMOUNT_PATTERN,
}
_SIGNATURE_WINDOW_HEADING = re.compile(r"签署|签字|签章|盖章")
_SIGNATURE_FIELD = re.compile(
    r"^(?:"
    r"(?:签署|签字|签章|盖章)页"
    r"|(?:甲|乙|丙|丁)方(?:[（(][^（）()]+[）)])?\s*[:：].*"
    r"|(?:法定代表人|授权代表|签署日期|日期)\s*[:：].*"
    r"|(?:法定代表人|授权代表)(?:[（(](?:签字|签章)[）)])?"
    r"|[（(](?:盖章|签章|签字)[）)]"
    r"|[（(]?以下无正文[）)]?"
    r")$"
)
_SIGNATURE_FIELD_SEPARATOR = re.compile(r"[|｜]")
_SIGNATURE_SUBSTANTIVE_CUES = re.compile(
    r"付款|支付|价款|费用|交付|验收|违约|赔偿|责任|解除|终止|保密|"
    r"知识产权|争议|仲裁|诉讼|权利|义务|应当|必须|不得|有权|承担|约定|生效|履行|期限"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedBlock(StrictModel):
    block_id: str = Field(min_length=1, max_length=160)
    text_length: int = Field(gt=0)


class PipelineWindowInput(WindowExtractionRequest):
    sequence_no: int = Field(ge=1)
    section_ids: list[str] = Field(min_length=1, max_length=500)
    heading_path: list[str] = Field(default_factory=list, max_length=30)
    clause_nos: list[str] = Field(default_factory=list, max_length=100)
    primary_block_ids: list[str] = Field(min_length=1, max_length=2_000)
    estimated_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_primary_blocks(self) -> "PipelineWindowInput":
        if len(self.primary_block_ids) != len(set(self.primary_block_ids)):
            raise ValueError("primary_block_ids must be unique inside one Window")
        offset_blocks = {item.block_id for item in self.offset_map}
        if set(self.primary_block_ids) != offset_blocks:
            raise ValueError("primary_block_ids must match offset_map block IDs")
        return self

    def extraction_request(self) -> WindowExtractionRequest:
        return WindowExtractionRequest.model_validate(
            self.model_dump(
                include={"window_id", "source_text", "context_text", "offset_map"}
            )
        )


class WindowPipelineRequest(StrictModel):
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    expected_blocks: list[ExpectedBlock] = Field(min_length=1, max_length=20_000)
    expected_section_ids: list[str] = Field(min_length=1, max_length=20_000)
    windows: list[PipelineWindowInput] = Field(min_length=1, max_length=5_000)
    concurrency: Literal[10] = WINDOW_EXTRACTION_CONCURRENCY

    @model_validator(mode="after")
    def validate_identity(self) -> "WindowPipelineRequest":
        block_ids = [item.block_id for item in self.expected_blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("expected_blocks contains duplicate block_id")
        if len(self.expected_section_ids) != len(set(self.expected_section_ids)):
            raise ValueError("expected_section_ids must be unique")
        window_ids = [item.window_id for item in self.windows]
        sequences = [item.sequence_no for item in self.windows]
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("window_id must be unique")
        if len(sequences) != len(set(sequences)):
            raise ValueError("window sequence_no must be unique")
        if sorted(sequences) != list(range(1, len(sequences) + 1)):
            raise ValueError("window sequence_no must be contiguous from 1")
        return self


class PipelineSourceAnchor(StrictModel):
    anchor_id: str
    block_id: str
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class PipelineIrDefinition(StrictModel):
    term: str = Field(min_length=1, max_length=500)
    meaning: str = Field(min_length=1, max_length=5_000)
    source_anchors: list[PipelineSourceAnchor] = Field(min_length=1)


class PipelineIrSemanticItem(StrictModel):
    item_id: str
    subject: str | None = Field(default=None, max_length=500)
    predicate: str = Field(min_length=1, max_length=1_000)
    object: str | None = Field(default=None, max_length=5_000)
    source_anchors: list[PipelineSourceAnchor] = Field(min_length=1)


class PipelineSemanticIr(StrictModel):
    definitions: list[PipelineIrDefinition] = Field(default_factory=list)
    rights: list[PipelineIrSemanticItem] = Field(default_factory=list)
    obligations: list[PipelineIrSemanticItem] = Field(default_factory=list)
    prohibitions: list[PipelineIrSemanticItem] = Field(default_factory=list)
    payment_terms: list[PipelineIrSemanticItem] = Field(default_factory=list)
    delivery_terms: list[PipelineIrSemanticItem] = Field(default_factory=list)
    acceptance_terms: list[PipelineIrSemanticItem] = Field(default_factory=list)
    liabilities: list[PipelineIrSemanticItem] = Field(default_factory=list)
    termination_terms: list[PipelineIrSemanticItem] = Field(default_factory=list)
    confidentiality_terms: list[PipelineIrSemanticItem] = Field(default_factory=list)
    intellectual_property_terms: list[PipelineIrSemanticItem] = Field(default_factory=list)
    dispute_resolution: list[PipelineIrSemanticItem] = Field(default_factory=list)
    dates: list[PipelineIrSemanticItem] = Field(default_factory=list)
    amounts: list[PipelineIrSemanticItem] = Field(default_factory=list)


class TechnicalAnchor(StrictModel):
    anchor_id: str
    document_id: str
    generation_id: str
    window_id: str
    block_id: str
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MappedExtraction(StrictModel):
    extraction_class: str
    output_field: str
    stable_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_order: tuple[int, int]
    clause_no: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    technical_anchors: list[TechnicalAnchor] = Field(min_length=1)
    contract_value: PipelineIrDefinition | PipelineIrSemanticItem


class WindowAttempt(StrictModel):
    attempt_no: int = Field(ge=1, le=2)
    duration_ms: int = Field(ge=0)
    status: Literal["SUCCEEDED", "FAILED", "SUSPICIOUS_EMPTY", "SUSPICIOUS_CATEGORY"]
    extraction_count: int = Field(ge=0)
    value_canonicalization_count: int = Field(default=0, ge=0)
    ambiguous_value_count: int = Field(default=0, ge=0)
    unbound_value_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    fallback_extraction_count: int = Field(default=0, ge=0)
    fallback_reason: str | None = Field(default=None, max_length=2_000)


class WindowRunResult(StrictModel):
    window_id: str
    sequence_no: int
    status: Literal["SUCCEEDED", "FAILED"]
    attempts: list[WindowAttempt] = Field(min_length=1, max_length=2)
    mapped_extractions: list[MappedExtraction] = Field(default_factory=list)


class PipelineCoverage(StrictModel):
    valid: bool
    expected_block_count: int = Field(ge=0)
    covered_block_count: int = Field(ge=0)
    expected_section_count: int = Field(ge=0)
    covered_section_count: int = Field(ge=0)
    expected_window_count: int = Field(ge=0)
    processed_window_count: int = Field(ge=0)
    missing_block_ids: list[str] = Field(default_factory=list)
    overlapping_block_ids: list[str] = Field(default_factory=list)
    incomplete_block_ids: list[str] = Field(default_factory=list)
    missing_section_ids: list[str] = Field(default_factory=list)
    unexpected_section_ids: list[str] = Field(default_factory=list)
    failed_window_ids: list[str] = Field(default_factory=list)


class WindowPipelineResult(StrictModel):
    document_id: str
    generation_id: str
    concurrency: Literal[10] = WINDOW_EXTRACTION_CONCURRENCY
    duration_ms: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    semantic_ir_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coverage: PipelineCoverage
    windows: list[WindowRunResult]
    semantic_ir: PipelineSemanticIr


class WindowPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class WindowExtractor(Protocol):
    async def extract(
        self,
        request: WindowExtractionRequest,
        *,
        tenant_id: str,
        model_id: str,
        retry_feedback: str | None = None,
    ) -> WindowExtractionResult: ...


@dataclass(slots=True)
class ContractIrWindowPipeline:
    extractor: WindowExtractor
    clock: Callable[[], float] = time.perf_counter
    max_concurrency: int = WINDOW_EXTRACTION_CONCURRENCY
    max_attempts_per_window: int = 2
    _active_calls: int = field(default=0, init=False)
    _max_observed_concurrency: int = field(default=0, init=False)

    async def run(
        self,
        request: WindowPipelineRequest,
        *,
        tenant_id: str,
        model_id: str,
    ) -> WindowPipelineResult:
        if (
            request.concurrency != self.max_concurrency
            or self.max_concurrency != WINDOW_EXTRACTION_CONCURRENCY
        ):
            raise WindowPipelineError(
                "WINDOW_CONCURRENCY_INVALID",
                "合同 IR Window 并发必须固定为 10",
                {"requested": request.concurrency, "configured": self.max_concurrency},
            )
        structural_coverage = _validate_structural_coverage(request)
        if not structural_coverage.valid:
            raise WindowPipelineError(
                "WINDOW_COVERAGE_INVALID",
                "Window Primary Source 覆盖不完整",
                structural_coverage.model_dump(mode="json"),
            )

        started = self.clock()
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def guarded(window: PipelineWindowInput) -> WindowRunResult:
            async with semaphore:
                self._active_calls += 1
                self._max_observed_concurrency = max(
                    self._max_observed_concurrency,
                    self._active_calls,
                )
                try:
                    return await self._run_window(
                        request,
                        window,
                        tenant_id=tenant_id,
                        model_id=model_id,
                    )
                finally:
                    self._active_calls -= 1

        window_results = await asyncio.gather(
            *(guarded(window) for window in sorted(request.windows, key=lambda item: item.sequence_no))
        )
        failed_ids = [item.window_id for item in window_results if item.status == "FAILED"]
        final_coverage = structural_coverage.model_copy(
            update={
                "valid": structural_coverage.valid and not failed_ids,
                "processed_window_count": len(window_results) - len(failed_ids),
                "failed_window_ids": failed_ids,
            }
        )
        # A single failed Window must not discard the validated IR produced by
        # every other Window. Still fail closed when nothing was extracted or
        # when failures are no longer isolated.
        all_windows_failed = len(failed_ids) == len(window_results)
        if len(failed_ids) > 1 or all_windows_failed:
            raise WindowPipelineError(
                "WINDOW_EXTRACTION_FAILED",
                "多个或全部 Window 在两次局部执行后仍失败，IR Stage 不返回残缺结果",
                {
                    "coverage": final_coverage.model_dump(mode="json"),
                    "windows": [item.model_dump(mode="json") for item in window_results],
                },
            )

        semantic_ir = _merge_mapped_extractions(window_results)
        semantic_dump = semantic_ir.model_dump(mode="json")
        attempts = [attempt for item in window_results for attempt in item.attempts]
        return WindowPipelineResult(
            document_id=request.document_id,
            generation_id=request.generation_id,
            duration_ms=round((self.clock() - started) * 1000),
            model_call_count=len(attempts),
            retry_count=sum(len(item.attempts) - 1 for item in window_results),
            semantic_ir_hash=_stable_hash(semantic_dump),
            coverage=final_coverage,
            windows=window_results,
            semantic_ir=semantic_ir,
        )

    async def _run_window(
        self,
        request: WindowPipelineRequest,
        window: PipelineWindowInput,
        *,
        tenant_id: str,
        model_id: str,
    ) -> WindowRunResult:
        attempts: list[WindowAttempt] = []
        retry_feedback: str | None = None
        accepted_extractions: list[AlignedExtraction] = []
        accepted_canonicalizations: list[ValueCanonicalization] = []
        for attempt_no in range(1, self.max_attempts_per_window + 1):
            started = self.clock()
            try:
                extracted = await self.extractor.extract(
                    window.extraction_request(),
                    tenant_id=tenant_id,
                    model_id=model_id,
                    retry_feedback=retry_feedback,
                )
                accepted_extractions = _merge_aligned_extractions(
                    accepted_extractions,
                    extracted.extractions,
                )
                accepted_canonicalizations = _merge_value_canonicalizations(
                    accepted_canonicalizations,
                    extracted.value_canonicalizations,
                )
                accumulated = extracted.model_copy(
                    update={
                        "extractions": accepted_extractions,
                        "value_canonicalizations": accepted_canonicalizations,
                    }
                )
                suspicious_empty = not accumulated.extractions and _is_suspicious_empty(window)
                duration_ms = round((self.clock() - started) * 1000)
                if suspicious_empty:
                    attempts.append(
                        WindowAttempt(
                            attempt_no=attempt_no,
                            duration_ms=duration_ms,
                            status="SUSPICIOUS_EMPTY",
                            extraction_count=0,
                            error_code="WINDOW_SUSPICIOUS_EMPTY",
                            error_message="关键条款 Window 返回空结果",
                        )
                    )
                    retry_feedback = "关键条款 Window 返回空结果"
                    if attempt_no == self.max_attempts_per_window:
                        recovered = _recover_with_source_fallback(
                            request=request,
                            window=window,
                            attempts=attempts,
                            accepted_extractions=accepted_extractions,
                            accepted_canonicalizations=accepted_canonicalizations,
                            fallback_reason="模型两次返回空结果，按原文生成保守回退映射",
                        )
                        if recovered is not None:
                            return recovered
                    continue
                missing_categories = _missing_expected_categories(window, accumulated)
                if missing_categories:
                    missing_text = "、".join(missing_categories)
                    attempts.append(
                        WindowAttempt(
                            attempt_no=attempt_no,
                            duration_ms=duration_ms,
                            status="SUSPICIOUS_CATEGORY",
                            extraction_count=len(accumulated.extractions),
                            value_canonicalization_count=len(
                                accumulated.value_canonicalizations
                            ),
                            ambiguous_value_count=sum(
                                item.binding_status == "AMBIGUOUS"
                                for item in accumulated.value_canonicalizations
                            ),
                            unbound_value_count=sum(
                                item.binding_status == "UNBOUND"
                                for item in accumulated.value_canonicalizations
                            ),
                            error_code="WINDOW_CATEGORY_MISSING",
                            error_message=f"强指示条款缺少类别：{missing_text}",
                        )
                    )
                    retry_feedback = (
                        f"当前 source_text 明确包含 {missing_text} 类别指示，但结果缺少对应类别；"
                        "类别可以共享同一 extraction_text；本次只返回缺少类别的增量项，"
                        "不要重复已经通过校验的其他抽取项"
                    )
                    if (
                        attempt_no == self.max_attempts_per_window
                        and set(missing_categories) == {"GOVERNING_LAW"}
                    ):
                        recovered = _recover_with_source_fallback(
                            request=request,
                            window=window,
                            attempts=attempts,
                            accepted_extractions=accepted_extractions,
                            accepted_canonicalizations=accepted_canonicalizations,
                            required_classes={"GOVERNING_LAW"},
                            fallback_reason="模型两次未返回适用法律类别，按原文生成保守回退映射",
                        )
                        if recovered is not None:
                            return recovered
                    continue
                mapped = [
                    _map_extraction(request, window, item)
                    for item in accumulated.extractions
                ]
                attempts.append(
                    WindowAttempt(
                        attempt_no=attempt_no,
                        duration_ms=duration_ms,
                        status="SUCCEEDED",
                        extraction_count=len(mapped),
                        value_canonicalization_count=len(
                            accumulated.value_canonicalizations
                        ),
                        ambiguous_value_count=sum(
                            item.binding_status == "AMBIGUOUS"
                            for item in accumulated.value_canonicalizations
                        ),
                        unbound_value_count=sum(
                            item.binding_status == "UNBOUND"
                            for item in accumulated.value_canonicalizations
                        ),
                    )
                )
                return WindowRunResult(
                    window_id=window.window_id,
                    sequence_no=window.sequence_no,
                    status="SUCCEEDED",
                    attempts=attempts,
                    mapped_extractions=mapped,
                )
            except Exception as exc:  # the second failure is reported, never returned as partial IR
                code = str(getattr(exc, "code", "WINDOW_EXECUTION_FAILED"))
                message = str(exc)[:2_000]
                accepted_extractions = _merge_aligned_extractions(
                    accepted_extractions,
                    list(getattr(exc, "accepted_extractions", [])),
                )
                accepted_canonicalizations = _merge_value_canonicalizations(
                    accepted_canonicalizations,
                    list(getattr(exc, "value_canonicalizations", [])),
                )
                attempts.append(
                    WindowAttempt(
                        attempt_no=attempt_no,
                        duration_ms=round((self.clock() - started) * 1000),
                        status="FAILED",
                        extraction_count=0,
                        error_code=code,
                        error_message=message,
                    )
                )
                retry_feedback = (
                    getattr(exc, "retry_feedback", None)
                    or f"{code}: {message}"
                )
                required_categories = _expected_categories(window)
                if required_categories:
                    required_text = "、".join(required_categories)
                    retry_feedback = (
                        f"{retry_feedback}；本次局部复查必须包含强指示类别：{required_text}"
                    )
                if accepted_extractions:
                    retry_feedback = (
                        f"{retry_feedback}；已有 {len(accepted_extractions)} 项通过严格原文校验并由"
                        "系统保留，本次只返回需要修复或补齐的增量项，不要重复已通过项"
                    )
                else:
                    retry_feedback = (
                        f"{retry_feedback}；当前没有可保留的已验证项，请重新返回当前 Window "
                        "的完整抽取结果"
                    )
                if (
                    attempt_no == self.max_attempts_per_window
                    and code == "WINDOW_ALIGNMENT_FAILED"
                ):
                    recovered = _recover_with_source_fallback(
                        request=request,
                        window=window,
                        attempts=attempts,
                        accepted_extractions=accepted_extractions,
                        accepted_canonicalizations=accepted_canonicalizations,
                        fallback_reason="模型两次原文定位失败，按原文生成保守回退映射",
                    )
                    if recovered is not None:
                        return recovered
        return WindowRunResult(
            window_id=window.window_id,
            sequence_no=window.sequence_no,
            status="FAILED",
            attempts=attempts,
        )

    @property
    def max_observed_concurrency(self) -> int:
        return self._max_observed_concurrency


def _merge_aligned_extractions(
    accepted: list[AlignedExtraction],
    incoming: list[AlignedExtraction],
) -> list[AlignedExtraction]:
    """Merge retry deltas without allowing a later call to rewrite accepted IR."""

    merged: dict[tuple[Any, ...], AlignedExtraction] = {}
    for item in [*accepted, *incoming]:
        key = (
            item.extraction_class,
            item.rendered_char_start,
            item.rendered_char_end,
            item.subject,
            item.predicate,
            item.object,
            item.term,
            item.meaning,
            tuple(item.referenced_clause_nos),
            tuple(
                (
                    span.block_id,
                    span.block_char_start,
                    span.block_char_end,
                )
                for span in item.source_spans
            ),
        )
        merged.setdefault(key, item)
    return sorted(
        merged.values(),
        key=lambda item: (
            item.rendered_char_start,
            item.extraction_class,
            item.extraction_text,
        ),
    )


def _merge_value_canonicalizations(
    accepted: list[ValueCanonicalization],
    incoming: list[ValueCanonicalization],
) -> list[ValueCanonicalization]:
    merged: dict[tuple[Any, ...], ValueCanonicalization] = {}
    for item in [*accepted, *incoming]:
        key = (
            item.extraction_class,
            item.extraction_text,
            item.predicate,
            item.binding_status,
            item.related_extraction_class,
        )
        merged.setdefault(key, item)
    return list(merged.values())


def _validate_structural_coverage(request: WindowPipelineRequest) -> PipelineCoverage:
    expected_lengths = {item.block_id: item.text_length for item in request.expected_blocks}
    ranges: dict[str, list[tuple[int, int]]] = {block_id: [] for block_id in expected_lengths}
    unexpected_blocks: set[str] = set()
    covered_sections: set[str] = set()
    for window in request.windows:
        covered_sections.update(window.section_ids)
        for offset in window.offset_map:
            if offset.block_id not in ranges:
                unexpected_blocks.add(offset.block_id)
                continue
            ranges[offset.block_id].append((offset.block_char_start, offset.block_char_end))

    missing: list[str] = []
    overlapping: list[str] = []
    incomplete: list[str] = []
    for block_id, text_length in expected_lengths.items():
        block_ranges = sorted(ranges[block_id])
        if not block_ranges:
            missing.append(block_id)
            continue
        cursor = 0
        invalid_overlap = False
        invalid_gap = False
        for start, end in block_ranges:
            if start < cursor:
                invalid_overlap = True
            elif start > cursor:
                invalid_gap = True
            cursor = max(cursor, end)
        if invalid_overlap:
            overlapping.append(block_id)
        if invalid_gap or cursor != text_length:
            incomplete.append(block_id)
    if unexpected_blocks:
        incomplete.extend(sorted(unexpected_blocks))

    expected_sections = set(request.expected_section_ids)
    missing_sections = sorted(expected_sections - covered_sections)
    unexpected_sections = sorted(covered_sections - expected_sections)
    valid = not any(
        (missing, overlapping, incomplete, missing_sections, unexpected_sections)
    )
    return PipelineCoverage(
        valid=valid,
        expected_block_count=len(expected_lengths),
        covered_block_count=len(expected_lengths) - len(missing),
        expected_section_count=len(expected_sections),
        covered_section_count=len(covered_sections & expected_sections),
        expected_window_count=len(request.windows),
        processed_window_count=0,
        missing_block_ids=sorted(missing),
        overlapping_block_ids=sorted(overlapping),
        incomplete_block_ids=sorted(set(incomplete)),
        missing_section_ids=missing_sections,
        unexpected_section_ids=unexpected_sections,
    )


def _is_suspicious_empty(window: PipelineWindowInput) -> bool:
    if _is_signature_only_window(window):
        return False
    searchable = _validation_text(window)
    return _CRITICAL_CONTENT.search(searchable) is not None


def _missing_expected_categories(
    window: PipelineWindowInput,
    extracted: WindowExtractionResult,
) -> list[str]:
    expected = _expected_categories(window)
    actual = {item.extraction_class for item in extracted.extractions}
    return [category for category in expected if category not in actual]


def _expected_categories(window: PipelineWindowInput) -> list[str]:
    searchable = _validation_text(window)
    return [
        category
        for category, pattern in _CATEGORY_CUES.items()
        if pattern.search(searchable) is not None
    ]


def _validation_text(window: PipelineWindowInput) -> str:
    """Return clause text for semantic validation without document preamble metadata."""

    source_text = window.source_text
    anchors = [*reversed(window.heading_path), *window.clause_nos]
    for anchor in anchors:
        normalized_anchor = " ".join(anchor.split())
        if not normalized_anchor:
            continue
        for match in re.finditer(r"(?m)^[^\n]*$", source_text):
            normalized_line = " ".join(match.group(0).split())
            if normalized_line == normalized_anchor or normalized_line.startswith(
                f"{normalized_anchor} "
            ):
                return source_text[match.start() :]
    # heading_path is contextual metadata and can be stale when a DOCX mixes
    # Word outline styles with literal article boundaries. A heading that is
    # absent from this Window's source cannot impose a hard semantic category;
    # otherwise the retry asks for text that strict alignment forbids inventing.
    return source_text


def _is_signature_only_window(window: PipelineWindowInput) -> bool:
    """Allow an empty result only for a window that contains signing fields alone."""

    heading_text = "\n".join(window.heading_path)
    if _SIGNATURE_WINDOW_HEADING.search(heading_text) is None:
        return False
    if _SIGNATURE_SUBSTANTIVE_CUES.search(window.source_text) is not None:
        return False
    fields = [
        " ".join(fragment.split())
        for line in window.source_text.splitlines()
        for fragment in _SIGNATURE_FIELD_SEPARATOR.split(line)
        if fragment.strip()
    ]
    return bool(fields) and all(_SIGNATURE_FIELD.fullmatch(field) for field in fields)


def _recover_with_source_fallback(
    *,
    request: WindowPipelineRequest,
    window: PipelineWindowInput,
    attempts: list[WindowAttempt],
    accepted_extractions: list[AlignedExtraction],
    accepted_canonicalizations: list[ValueCanonicalization],
    fallback_reason: str,
    required_classes: set[str] | None = None,
) -> WindowRunResult | None:
    """Recover a failed Window from exact source text without invented facts."""
    fallback = _source_fallback_extractions(
        window,
        required_classes=required_classes,
    )
    if not fallback:
        return None
    merged_extractions = _merge_aligned_extractions(accepted_extractions, fallback)
    accumulated = WindowExtractionResult(
        window_id=window.window_id,
        model_id="source-fallback",
        extractions=merged_extractions,
        value_canonicalizations=accepted_canonicalizations,
    )
    if _missing_expected_categories(window, accumulated):
        return None
    mapped = [_map_extraction(request, window, item) for item in merged_extractions]
    previous = attempts[-1]
    attempts[-1] = previous.model_copy(
        update={
            "status": "SUCCEEDED",
            "extraction_count": len(mapped),
            "value_canonicalization_count": len(accepted_canonicalizations),
            "ambiguous_value_count": sum(
                item.binding_status == "AMBIGUOUS"
                for item in accepted_canonicalizations
            ),
            "unbound_value_count": sum(
                item.binding_status == "UNBOUND"
                for item in accepted_canonicalizations
            ),
            "error_code": None,
            "error_message": None,
            "fallback_extraction_count": len(fallback),
            "fallback_reason": fallback_reason,
        }
    )
    return WindowRunResult(
        window_id=window.window_id,
        sequence_no=window.sequence_no,
        status="SUCCEEDED",
        attempts=attempts,
        mapped_extractions=mapped,
    )


def _source_fallback_extractions(
    window: PipelineWindowInput,
    *,
    required_classes: set[str] | None = None,
) -> list[AlignedExtraction]:
    """Map exact source blocks after two failed model attempts.

    The fallback does not infer parties or legal effects. Its predicate marks
    the result as a source recovery so downstream review sees the actual text.
    """
    result: list[AlignedExtraction] = []
    for offset in window.offset_map:
        text = window.source_text[offset.rendered_start : offset.rendered_end]
        candidates = [
            extraction_class
            for extraction_class, pattern in _FALLBACK_CLASS_CUES.items()
            if pattern.search(text)
            and (required_classes is None or extraction_class in required_classes)
        ]
        if (
            not candidates
            and required_classes is None
            and _CRITICAL_CONTENT.search(text)
        ):
            candidates = ["OBLIGATION"]
        for extraction_class in candidates:
            values = [None]
            if extraction_class == "DATE":
                values = [match.group(0) for match in EXPLICIT_DATE_PATTERN.finditer(text)]
            elif extraction_class == "AMOUNT":
                values = [match.group(0) for match in EXPLICIT_AMOUNT_PATTERN.finditer(text)]
            for value in values:
                result.append(
                    AlignedExtraction(
                        extraction_class=extraction_class,
                        extraction_text=text,
                        subject=None,
                        predicate=(
                            "时间约束为"
                            if extraction_class == "DATE"
                            else (
                                "数值约束为"
                                if extraction_class == "AMOUNT"
                                else "原文待模型复核"
                            )
                        ),
                        object=value,
                        term=None,
                        meaning=None,
                        referenced_clause_nos=list(window.clause_nos),
                        rendered_char_start=offset.rendered_start,
                        rendered_char_end=offset.rendered_end,
                        alignment_status="MATCH_EXACT",
                        source_spans=[
                            SourceSpan(
                                block_id=offset.block_id,
                                block_no=offset.block_no,
                                block_char_start=offset.block_char_start,
                                block_char_end=offset.block_char_end,
                                quoted_text=text,
                            )
                        ],
                    )
                )
    return result


def _map_extraction(
    request: WindowPipelineRequest,
    window: PipelineWindowInput,
    extraction: AlignedExtraction,
) -> MappedExtraction:
    technical_anchors: list[TechnicalAnchor] = []
    contract_anchors: list[PipelineSourceAnchor] = []
    for span in extraction.source_spans:
        page_number = _page_number_for_span(window, span.block_id, span.block_char_start, span.block_char_end)
        anchor_id = _stable_id(
            "anchor",
            request.generation_id,
            span.block_id,
            str(span.block_char_start),
            str(span.block_char_end),
        )
        quoted_hash = _stable_hash_text(span.quoted_text)
        technical_anchors.append(
            TechnicalAnchor(
                anchor_id=anchor_id,
                document_id=request.document_id,
                generation_id=request.generation_id,
                window_id=window.window_id,
                block_id=span.block_id,
                page_number=page_number,
                char_start=span.block_char_start,
                char_end=span.block_char_end,
                quoted_text=span.quoted_text,
                quoted_text_hash=quoted_hash,
            )
        )
        contract_anchors.append(
            PipelineSourceAnchor(
                anchor_id=anchor_id,
                block_id=span.block_id,
                page_number=page_number,
                char_start=span.block_char_start,
                char_end=span.block_char_end,
            )
        )

    # Source identity remains anchored to immutable text, while the semantic
    # fields distinguish multiple legal facts legitimately expressed by the
    # same continuous clause. Fully identical outputs still share one hash and
    # are deterministically de-duplicated.
    stable_payload = {
        "extraction_class": extraction.extraction_class,
        "extraction_text": extraction.extraction_text,
        "subject": extraction.subject,
        "predicate": extraction.predicate,
        "object": extraction.object,
        "term": extraction.term,
        "meaning": extraction.meaning,
        "referenced_clause_nos": extraction.referenced_clause_nos,
        "anchors": [
            [item.block_id, item.char_start, item.char_end] for item in technical_anchors
        ],
    }
    stable_hash = _stable_hash(stable_payload)
    if extraction.extraction_class == "DEFINITION":
        assert extraction.term is not None and extraction.meaning is not None
        contract_value: PipelineIrDefinition | PipelineIrSemanticItem = PipelineIrDefinition(
            term=extraction.term,
            meaning=extraction.meaning,
            source_anchors=contract_anchors,
        )
    else:
        assert extraction.predicate is not None
        contract_value = PipelineIrSemanticItem(
            item_id=_stable_id("ir-item", stable_hash),
            subject=extraction.subject,
            predicate=extraction.predicate,
            object=extraction.object,
            source_anchors=contract_anchors,
        )
    return MappedExtraction(
        extraction_class=extraction.extraction_class,
        output_field=IR_FIELD_BY_CLASS[extraction.extraction_class],
        stable_hash=stable_hash,
        source_order=(window.sequence_no, extraction.rendered_char_start),
        clause_no=window.clause_nos[0] if window.clause_nos else None,
        heading_path=window.heading_path,
        technical_anchors=technical_anchors,
        contract_value=contract_value,
    )


def _page_number_for_span(
    window: PipelineWindowInput,
    block_id: str,
    char_start: int,
    char_end: int,
) -> int | None:
    matches = [
        item
        for item in window.offset_map
        if item.block_id == block_id
        and item.block_char_start <= char_start
        and item.block_char_end >= char_end
    ]
    if len(matches) != 1:
        raise ValueError("Aligned source span must map to exactly one Window offset")
    return matches[0].page_number


def _merge_mapped_extractions(windows: list[WindowRunResult]) -> PipelineSemanticIr:
    merged: dict[str, list[PipelineIrDefinition | PipelineIrSemanticItem]] = {
        field_name: [] for field_name in IR_FIELDS
    }
    seen: set[str] = set()
    ordered = sorted(
        (item for window in windows for item in window.mapped_extractions),
        key=lambda item: (
            item.source_order[0],
            item.source_order[1],
            item.extraction_class,
            item.stable_hash,
        ),
    )
    for item in ordered:
        if item.stable_hash in seen:
            continue
        seen.add(item.stable_hash)
        merged[item.output_field].append(item.contract_value)
    return PipelineSemanticIr.model_validate(merged)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _stable_hash_text(canonical)


def _stable_hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ContractIrWindowPipeline",
    "ExpectedBlock",
    "MappedExtraction",
    "PipelineCoverage",
    "PipelineSemanticIr",
    "PipelineWindowInput",
    "WindowPipelineError",
    "WindowPipelineRequest",
    "WindowPipelineResult",
]
