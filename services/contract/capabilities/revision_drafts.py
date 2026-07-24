"""Independent revision-draft MVP built on completed contract-review findings.

This module is deliberately outside the formal review pipeline.  It consumes a
frozen result plus the Contract IR used by that result and never mutates either.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
PROMPT_TARGET_TOKENS = 6_000
PROMPT_HARD_LIMIT_TOKENS = 7_000
MAX_BATCH_FINDINGS = 6
_PLACEHOLDER_RE = re.compile(r"(?:TODO|TBD|XXX|待补充|待定|请填写)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
)
_AMOUNT_RE = re.compile(
    r"(?:人民币\s*)?(?:(?:¥|￥)\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:元|万元|亿元|%|％))"
)
_COMPANY_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·]{4,80}(?:有限责任公司|有限公司|股份有限公司)")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionIrSource(StrictModel):
    ir_id: str = Field(min_length=1, max_length=200)
    anchor_id: str = Field(min_length=1, max_length=200)
    block_id: str = Field(min_length=1, max_length=200)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    extraction_text: str = Field(min_length=1)
    context_text: str = ""

    @model_validator(mode="after")
    def validate_range(self) -> "RevisionIrSource":
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class RevisionEvidenceSource(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=200)
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]
    block_id: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, gt=0)
    quoted_text: str | None = None
    quoted_text_hash: str | None = None
    checked_scope: str | None = None


class RevisionFindingSource(StrictModel):
    finding_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    risk_level: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    check_code: str | None = None
    risk_type: str | None = None
    control_codes: list[str] = Field(default_factory=list)
    preferred_operation: Literal["REPLACE", "DELETE"] | None = None


class RevisionReviewSource(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    review_id: str = Field(min_length=1, max_length=200)
    generation_id: str = Field(min_length=1, max_length=200)
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_status: Literal["COMPLETED"]
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1)
    counterparty: str = Field(min_length=1)
    findings: list[RevisionFindingSource]
    evidences: list[RevisionEvidenceSource]
    contract_ir: list[RevisionIrSource]


class RevisionTarget(StrictModel):
    ir_id: str
    anchor_id: str
    block_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text_hash: str


class RevisionDraft(StrictModel):
    revision_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    finding_id: str
    operation: Literal["REPLACE", "DELETE", "UNSUPPORTED"]
    original_text: str | None = None
    replacement_text: str | None = None
    change_reason: str
    target: RevisionTarget | None = None
    unsupported_reason: str | None = None
    revision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_status: Literal["VALID"]
    draft_note: str | None = None

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "RevisionDraft":
        if self.operation == "REPLACE":
            if not self.original_text or not self.replacement_text or self.target is None:
                raise ValueError("REPLACE requires original_text, replacement_text and target")
        elif self.operation == "DELETE":
            if not self.original_text or self.replacement_text is not None or self.target is None:
                raise ValueError("DELETE requires target and null replacement_text")
        elif not self.unsupported_reason:
            raise ValueError("UNSUPPORTED requires unsupported_reason")
        return self


class FailedRevisionFinding(StrictModel):
    finding_id: str
    error_code: Literal[
        "FINDING_NOT_FOUND",
        "FINDING_SOURCE_NOT_UNIQUE",
        "SOURCE_TEXT_INVALID",
        "REVISION_GENERATION_FAILED",
    ]
    message: str


class RevisionDraftResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    review_id: str
    generation_id: str
    result_hash: str
    status: Literal["PENDING", "GENERATING", "COMPLETED", "PARTIAL_FAILED", "FAILED"]
    drafts: list[RevisionDraft]
    failed_findings: list[FailedRevisionFinding]
    model_call_count: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    cache_hit: bool = False


class RevisionDraftError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class RevisionSourceProvider(Protocol):
    async def get_source(
        self,
        review_id: str,
        generation_id: str,
        result_hash: str,
    ) -> RevisionReviewSource: ...


class RevisionTextGenerator(Protocol):
    async def generate(
        self,
        items: Sequence["ReplacementRequest"],
        *,
        source: RevisionReviewSource,
    ) -> "ReplacementBatchResult": ...


class RevisionDraftCache(Protocol):
    async def get(self, key: str) -> RevisionDraftResponse | None: ...

    async def put(self, key: str, value: RevisionDraftResponse) -> None: ...


@dataclass(frozen=True, slots=True)
class ReplacementRequest:
    revision_key: str
    finding: RevisionFindingSource
    original_text: str
    target: RevisionTarget
    adjacent_context: str


@dataclass(frozen=True, slots=True)
class GeneratedReplacement:
    revision_key: str
    replacement_text: str
    draft_note: str | None = None


@dataclass(frozen=True, slots=True)
class ReplacementBatchResult:
    items: tuple[GeneratedReplacement, ...]
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(slots=True)
class InMemoryRevisionDraftCache:
    _values: dict[str, RevisionDraftResponse] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self, key: str) -> RevisionDraftResponse | None:
        async with self._lock:
            value = self._values.get(key)
            return value.model_copy(deep=True) if value is not None else None

    async def put(self, key: str, value: RevisionDraftResponse) -> None:
        async with self._lock:
            self._values[key] = value.model_copy(deep=True)


@dataclass(slots=True)
class FileRevisionDraftCache:
    """Small process-safe-enough MVP cache, isolated from the formal Result Sink."""

    root: Path
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self, key: str) -> RevisionDraftResponse | None:
        path = self._path(key)
        async with self._lock:
            if not path.exists():
                return None
            return RevisionDraftResponse.model_validate_json(path.read_text(encoding="utf-8"))

    async def put(self, key: str, value: RevisionDraftResponse) -> None:
        path = self._path(key)
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(value.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(path)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"


@dataclass(slots=True)
class InMemoryRevisionSourceProvider:
    sources: dict[tuple[str, str, str], RevisionReviewSource]

    async def register_source(self, source: RevisionReviewSource) -> None:
        self.sources[(source.review_id, source.generation_id, source.result_hash)] = (
            source.model_copy(deep=True)
        )

    async def get_source(
        self,
        review_id: str,
        generation_id: str,
        result_hash: str,
    ) -> RevisionReviewSource:
        exact = self.sources.get((review_id, generation_id, result_hash))
        if exact is not None:
            return exact.model_copy(deep=True)
        same_review = [key for key in self.sources if key[0] == review_id]
        if not same_review:
            raise RevisionDraftError("REVIEW_NOT_FOUND", "Contract review was not found", status_code=404)
        same_generation = [key for key in same_review if key[1] == generation_id]
        if not same_generation:
            raise RevisionDraftError("GENERATION_NOT_FOUND", "Review generation was not found", status_code=404)
        raise RevisionDraftError(
            "RESULT_HASH_MISMATCH",
            "result_hash does not match the completed review result",
            status_code=409,
        )


@dataclass(slots=True)
class JsonRevisionSourceProvider:
    """Read-only bridge for a completed formal payload and its frozen IR."""

    formal_payload_path: Path
    contract_ir_path: Path
    generation_id: str
    _source: RevisionReviewSource | None = None

    async def get_source(
        self,
        review_id: str,
        generation_id: str,
        result_hash: str,
    ) -> RevisionReviewSource:
        if self._source is None:
            try:
                payload = json.loads(self.formal_payload_path.read_text(encoding="utf-8"))
                ir_payload = json.loads(self.contract_ir_path.read_text(encoding="utf-8"))
                ir_values = _find_contract_ir_list(ir_payload)
                self._source = source_from_formal_payload(
                    payload,
                    generation_id=self.generation_id,
                    contract_ir=ir_values,
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RevisionDraftError(
                    "REVISION_GENERATION_FAILED",
                    f"Unable to load completed revision source: {exc}",
                    status_code=500,
                ) from exc
        source = self._source
        assert source is not None
        if source.review_id != review_id:
            raise RevisionDraftError("REVIEW_NOT_FOUND", "Contract review was not found", status_code=404)
        if source.generation_id != generation_id:
            raise RevisionDraftError("GENERATION_NOT_FOUND", "Review generation was not found", status_code=404)
        if source.result_hash != result_hash:
            raise RevisionDraftError("RESULT_HASH_MISMATCH", "Result hash mismatch", status_code=409)
        return source.model_copy(deep=True)


@dataclass(slots=True)
class LlmRevisionTextGenerator:
    tenant_id: str
    model_id: str
    runtime: Any | None = None

    async def generate(
        self,
        items: Sequence[ReplacementRequest],
        *,
        source: RevisionReviewSource,
    ) -> ReplacementBatchResult:
        if self.runtime is None:
            from service.conversation.llm_runner import LlmRuntime

            runtime = LlmRuntime(self.tenant_id)
        else:
            runtime = self.runtime
        payload = {
            "task": "GENERATE_CONTRACT_REPLACEMENT_TEXT_ONLY",
            "parties": {
                "our_party": source.our_party,
                "counterparty": source.counterparty,
                "perspective": source.perspective,
            },
            "output_schema": {
                "drafts": [
                    {
                        "revision_key": "sha256:...",
                        "replacement_text": "string",
                        "draft_note": "optional string",
                    }
                ]
            },
            "constraints": [
                "逐项返回且只返回replacement_text，可选draft_note",
                "不得重新判断风险，不得改变事实、主体、金额、日期或合同对象",
                "不得返回finding_id、operation、Evidence位置或风险等级",
                "不得使用XXX、TODO、待补充、待定等占位符",
                "修改必须直接处理给定风险根因并保持可直接替换原条款",
            ],
            "draft_requests": [
                {
                    "revision_key": item.revision_key,
                    "risk_root": item.finding.issue,
                    "formal_suggestion": item.finding.suggestion,
                    "control_codes": item.finding.control_codes,
                    "original_clause": item.original_text,
                    "adjacent_context": item.adjacent_context,
                    "facts_that_must_not_change": {
                        "our_party": source.our_party,
                        "counterparty": source.counterparty,
                        "amounts": sorted(_extract_values(_AMOUNT_RE, item.original_text)),
                        "dates": sorted(_extract_values(_DATE_RE, item.original_text)),
                    },
                }
                for item in items
            ],
        }
        completion = await _complete_revision_batch(
            runtime,
            tenant_id=self.tenant_id,
            model_id=self.model_id,
            user_prompt=_canonical_json(payload),
        )
        if completion.prompt_tokens is not None and completion.prompt_tokens > PROMPT_HARD_LIMIT_TOKENS:
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                f"Provider prompt tokens {completion.prompt_tokens} exceeded {PROMPT_HARD_LIMIT_TOKENS}",
            )
        try:
            raw = json.loads(completion.content)
            if not isinstance(raw, dict) or set(raw) != {"drafts"}:
                raise TypeError("top-level object must contain only drafts")
            values = raw["drafts"]
            if not isinstance(values, list):
                raise TypeError("drafts must be a list")
            parsed = []
            for value in values:
                if not isinstance(value, dict) or not set(value).issubset(
                    {"revision_key", "replacement_text", "draft_note"}
                ):
                    raise TypeError("draft item contains an unknown field")
                if not {"revision_key", "replacement_text"}.issubset(value):
                    raise TypeError("draft item is missing a required field")
                parsed.append(
                    GeneratedReplacement(
                        revision_key=str(value["revision_key"]),
                        replacement_text=str(value["replacement_text"]).strip(),
                        draft_note=(
                            str(value["draft_note"]).strip()
                            if value.get("draft_note") is not None
                            else None
                        ),
                    )
                )
            generated = tuple(parsed)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                f"Revision model returned invalid JSON: {exc}",
            ) from exc
        expected = [item.revision_key for item in items]
        actual = [item.revision_key for item in generated]
        if actual != expected or len(actual) != len(set(actual)):
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                "Revision model did not return every revision_key exactly once",
            )
        return ReplacementBatchResult(
            items=generated,
            prompt_tokens=completion.prompt_tokens,
            cached_tokens=completion.cached_tokens,
            completion_tokens=completion.completion_tokens,
        )


@dataclass(slots=True)
class RevisionDraftService:
    source_provider: RevisionSourceProvider
    generator: RevisionTextGenerator
    cache: RevisionDraftCache = field(default_factory=InMemoryRevisionDraftCache)
    max_batch_findings: int = MAX_BATCH_FINDINGS
    target_prompt_tokens: int = PROMPT_TARGET_TOKENS

    async def get_or_generate(
        self,
        review_id: str,
        generation_id: str,
        result_hash: str,
    ) -> RevisionDraftResponse:
        cache_key = _cache_key(review_id, generation_id, result_hash)
        cached = await self.cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True})
        source = await self.source_provider.get_source(review_id, generation_id, result_hash)
        _validate_source_identity(source, review_id, generation_id, result_hash)
        started = time.perf_counter()
        response = await self._generate(source)
        response = response.model_copy(
            update={"duration_ms": round((time.perf_counter() - started) * 1000)}
        )
        await self.cache.put(cache_key, response)
        return response

    async def _generate(self, source: RevisionReviewSource) -> RevisionDraftResponse:
        evidence_by_id = {item.evidence_id: item for item in source.evidences}
        drafts: list[RevisionDraft] = []
        failures: list[FailedRevisionFinding] = []
        replacements: list[ReplacementRequest] = []
        for finding in source.findings:
            revision_key = compute_revision_key(
                source.review_id,
                source.generation_id,
                source.result_hash,
                finding.finding_id,
            )
            evidences = [evidence_by_id.get(item) for item in finding.evidence_ids]
            if any(item is None for item in evidences):
                failures.append(
                    FailedRevisionFinding(
                        finding_id=finding.finding_id,
                        error_code="FINDING_NOT_FOUND",
                        message="Finding references an unknown Evidence",
                    )
                )
                continue
            selected = [item for item in evidences if item is not None]
            planned = _plan_draft(source, finding, selected, revision_key)
            if isinstance(planned, RevisionDraft):
                drafts.append(planned)
            elif isinstance(planned, FailedRevisionFinding):
                failures.append(planned)
            else:
                replacements.append(planned)

        model_calls = 0
        for batch in _build_batches(
            replacements,
            max_items=self.max_batch_findings,
            target_tokens=self.target_prompt_tokens,
        ):
            try:
                result = await self.generator.generate(batch, source=source)
                model_calls += 1
                generated_by_key = {item.revision_key: item for item in result.items}
                for item in batch:
                    generated = generated_by_key[item.revision_key]
                    try:
                        replacement = _validate_replacement(
                            generated.replacement_text,
                            item.original_text,
                            source,
                        )
                        drafts.append(
                            _draft(
                                revision_key=item.revision_key,
                                finding=item.finding,
                                operation="REPLACE",
                                original_text=item.original_text,
                                replacement_text=replacement,
                                target=item.target,
                                draft_note=generated.draft_note,
                            )
                        )
                    except RevisionDraftError as exc:
                        failures.append(
                            FailedRevisionFinding(
                                finding_id=item.finding.finding_id,
                                error_code="REVISION_GENERATION_FAILED",
                                message=str(exc),
                            )
                        )
            except RevisionDraftError as exc:
                model_calls += 1
                failures.extend(
                    FailedRevisionFinding(
                        finding_id=item.finding.finding_id,
                        error_code="REVISION_GENERATION_FAILED",
                        message=str(exc),
                    )
                    for item in batch
                )

        drafts.sort(key=lambda item: item.finding_id)
        failures.sort(key=lambda item: item.finding_id)
        if failures and drafts:
            status = "PARTIAL_FAILED"
        elif failures:
            status = "FAILED"
        else:
            status = "COMPLETED"
        return RevisionDraftResponse(
            review_id=source.review_id,
            generation_id=source.generation_id,
            result_hash=source.result_hash,
            status=status,
            drafts=drafts,
            failed_findings=failures,
            model_call_count=model_calls,
        )


def compute_revision_key(
    review_id: str,
    generation_id: str,
    result_hash: str,
    finding_id: str,
) -> str:
    return _sha256(review_id + generation_id + result_hash + finding_id)


def compute_revision_hash(
    revision_key: str,
    operation: str,
    original_text: str | None,
    replacement_text: str | None,
    target: RevisionTarget | None,
) -> str:
    return _sha256(
        _canonical_json(
            {
                "revision_key": revision_key,
                "operation": operation,
                "original_text": original_text,
                "replacement_text": replacement_text,
                "target": target.model_dump(mode="json") if target is not None else None,
            }
        )
    )


def source_from_formal_payload(
    payload: dict[str, Any],
    *,
    generation_id: str,
    contract_ir: Sequence[dict[str, Any]],
) -> RevisionReviewSource:
    """Adapt the frozen formal result without changing its DTO or hash."""

    evidences = [
        RevisionEvidenceSource(
            evidence_id=item["evidence_id"],
            evidence_type=item["evidence_type"],
            block_id=item.get("block_id"),
            char_start=item.get("char_start"),
            char_end=item.get("char_end"),
            quoted_text=item.get("quoted_text"),
            quoted_text_hash=item.get("quoted_text_hash"),
            checked_scope=item.get("checked_scope"),
        )
        for item in payload["evidences"]
    ]
    findings = [
        RevisionFindingSource(
            finding_id=item["finding_id"],
            title=item["title"],
            issue=item["issue"],
            suggestion=item["suggestion"],
            risk_level=item["risk_level"],
            evidence_ids=item["evidence_ids"],
        )
        for item in payload["findings"]
    ]
    ir_values = [
        adapted
        for item in contract_ir
        for adapted in _adapt_ir_values(item)
    ]
    profile = payload["contract_profile"]
    return RevisionReviewSource(
        review_id=payload["review_id"],
        generation_id=generation_id,
        result_hash=payload["result_hash"],
        review_status="COMPLETED",
        perspective=profile["perspective"],
        our_party=profile["our_party"],
        counterparty=profile["counterparty"],
        findings=findings,
        evidences=evidences,
        contract_ir=ir_values,
    )


def _find_contract_ir_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        if any(
            "ir_id" in item
            or "item_id" in item
            or "anchor" in item
            or "source_anchors" in item
            for item in value
        ):
            return value
    if isinstance(value, dict):
        for key in ("contract_ir", "items", "ir_items", "contract_ir_items"):
            child = value.get(key)
            if isinstance(child, list):
                return child
        collected: list[dict[str, Any]] = []
        for child in value.values():
            try:
                collected.extend(_find_contract_ir_list(child))
            except ValueError:
                continue
        if collected:
            return collected
    raise ValueError("Contract IR list was not found")


def _adapt_ir_values(item: dict[str, Any]) -> list[RevisionIrSource]:
    anchors = item.get("source_anchors")
    if not isinstance(anchors, list) or not anchors:
        anchors = [item.get("anchor") or {}]
    excerpt = (
        item.get("extraction_text")
        or item.get("source_excerpt")
        or item.get("text")
        or " ".join(
            str(item.get(key, "")).strip()
            for key in ("subject", "predicate", "object")
            if str(item.get(key, "")).strip()
        )
    )
    values = []
    for anchor in anchors:
        values.append(
            RevisionIrSource(
                ir_id=item.get("ir_id") or item.get("item_id") or item.get("id"),
                anchor_id=item.get("anchor_id") or anchor.get("anchor_id"),
                block_id=item.get("block_id") or anchor.get("block_id"),
                char_start=item.get("char_start", anchor.get("char_start", 0)),
                char_end=item.get("char_end", anchor.get("char_end", len(excerpt or ""))),
                extraction_text=excerpt,
                context_text=item.get("context_text") or item.get("source_context") or "",
            )
        )
    return values


def _plan_draft(
    source: RevisionReviewSource,
    finding: RevisionFindingSource,
    evidences: Sequence[RevisionEvidenceSource],
    revision_key: str,
) -> RevisionDraft | FailedRevisionFinding | ReplacementRequest:
    if any(item.evidence_type == "ABSENCE" for item in evidences):
        return _unsupported(
            revision_key,
            finding,
            "该风险包含缺失性证据，无法通过单段原文替换完成。",
        )
    text_evidence = [
        item
        for item in evidences
        if item.evidence_type in {"TEXT_QUOTE", "CONTEXT"} and item.quoted_text
    ]
    if len(text_evidence) != 1:
        return _unsupported(
            revision_key,
            finding,
            "该风险涉及多个或无法唯一确定的原文位置，V1不进行联动修改。",
        )
    evidence = text_evidence[0]
    try:
        target, adjacent_context = _resolve_target(source, evidence)
    except RevisionDraftError as exc:
        return FailedRevisionFinding(
            finding_id=finding.finding_id,
            error_code=(
                "SOURCE_TEXT_INVALID"
                if exc.code == "SOURCE_TEXT_INVALID"
                else "FINDING_SOURCE_NOT_UNIQUE"
            ),
            message=str(exc),
        )
    assert evidence.quoted_text is not None
    if finding.preferred_operation == "DELETE":
        return _draft(
            revision_key=revision_key,
            finding=finding,
            operation="DELETE",
            original_text=evidence.quoted_text,
            replacement_text=None,
            target=target,
        )
    return ReplacementRequest(
        revision_key=revision_key,
        finding=finding,
        original_text=evidence.quoted_text,
        target=target,
        adjacent_context=adjacent_context,
    )


def _resolve_target(
    source: RevisionReviewSource,
    evidence: RevisionEvidenceSource,
) -> tuple[RevisionTarget, str]:
    if (
        evidence.block_id is None
        or evidence.char_start is None
        or evidence.char_end is None
        or evidence.quoted_text is None
        or evidence.quoted_text_hash is None
    ):
        raise RevisionDraftError("SOURCE_TEXT_INVALID", "Text Evidence positioning is incomplete")
    expected_hash = _sha256(evidence.quoted_text)
    if evidence.quoted_text_hash != expected_hash:
        raise RevisionDraftError("SOURCE_TEXT_INVALID", "quoted_text_hash does not match original_text")
    if evidence.char_end - evidence.char_start != len(evidence.quoted_text):
        raise RevisionDraftError("SOURCE_TEXT_INVALID", "Evidence range length does not match original_text")
    candidates = []
    for item in source.contract_ir:
        if item.block_id != evidence.block_id:
            continue
        if item.char_start > evidence.char_start or item.char_end < evidence.char_end:
            continue
        # Formal Evidence has already been hash- and block-validated.  IR
        # coordinates therefore provide the authoritative Evidence-to-Anchor
        # relationship even when the compact semantic IR stores only an SPO
        # projection rather than the full source excerpt.
        if item.char_start <= evidence.char_start and item.char_end >= evidence.char_end:
            candidates.append(item)
            continue
        relative_start = evidence.char_start - item.char_start
        relative_end = relative_start + len(evidence.quoted_text)
        if item.extraction_text[relative_start:relative_end] == evidence.quoted_text:
            candidates.append(item)
            continue
        if evidence.quoted_text in item.extraction_text:
            candidates.append(item)
    unique = {(item.ir_id, item.anchor_id): item for item in candidates}
    if len(unique) != 1:
        raise RevisionDraftError(
            "FINDING_SOURCE_NOT_UNIQUE",
            "Finding Evidence does not resolve to exactly one IR/Anchor",
        )
    item = next(iter(unique.values()))
    return (
        RevisionTarget(
            ir_id=item.ir_id,
            anchor_id=item.anchor_id,
            block_id=evidence.block_id,
            char_start=evidence.char_start,
            char_end=evidence.char_end,
            quoted_text_hash=evidence.quoted_text_hash,
        ),
        item.context_text,
    )


def _validate_replacement(
    value: str,
    original_text: str,
    source: RevisionReviewSource,
) -> str:
    normalized = value.strip()
    if not normalized:
        raise RevisionDraftError("REVISION_GENERATION_FAILED", "replacement_text is empty")
    if normalized == original_text.strip():
        raise RevisionDraftError("REVISION_GENERATION_FAILED", "replacement_text did not change")
    if _PLACEHOLDER_RE.search(normalized):
        raise RevisionDraftError("REVISION_GENERATION_FAILED", "replacement_text contains a placeholder")
    if len(normalized) > max(1_200, len(original_text) * 8):
        raise RevisionDraftError("REVISION_GENERATION_FAILED", "replacement_text expanded abnormally")
    original_amounts = _extract_values(_AMOUNT_RE, original_text)
    replacement_amounts = _extract_values(_AMOUNT_RE, normalized)
    if not replacement_amounts.issubset(original_amounts):
        raise RevisionDraftError("REVISION_GENERATION_FAILED", "replacement_text introduced a new amount")
    original_dates = _extract_values(_DATE_RE, original_text)
    replacement_dates = _extract_values(_DATE_RE, normalized)
    if not replacement_dates.issubset(original_dates):
        raise RevisionDraftError("REVISION_GENERATION_FAILED", "replacement_text introduced a new date")
    known_names = {source.our_party, source.counterparty}
    for name in known_names:
        if name in original_text and name not in normalized:
            raise RevisionDraftError(
                "REVISION_GENERATION_FAILED",
                "replacement_text removed a named contract party",
            )
    original_companies = set(_COMPANY_RE.findall(original_text))
    replacement_companies = set(_COMPANY_RE.findall(normalized))
    if not replacement_companies.issubset(original_companies | known_names):
        raise RevisionDraftError(
            "REVISION_GENERATION_FAILED",
            "replacement_text introduced an unknown contract party",
        )
    return normalized


def _build_batches(
    values: Sequence[ReplacementRequest],
    *,
    max_items: int,
    target_tokens: int,
) -> list[list[ReplacementRequest]]:
    batches: list[list[ReplacementRequest]] = []
    current: list[ReplacementRequest] = []
    current_tokens = 800
    for item in values:
        estimated = _estimate_tokens(
            item.original_text
            + item.finding.issue
            + item.finding.suggestion
            + item.adjacent_context
        ) + 180
        if current and (len(current) >= max_items or current_tokens + estimated > target_tokens):
            batches.append(current)
            current = []
            current_tokens = 800
        current.append(item)
        current_tokens += estimated
    if current:
        batches.append(current)
    return batches


def _draft(
    *,
    revision_key: str,
    finding: RevisionFindingSource,
    operation: Literal["REPLACE", "DELETE"],
    original_text: str,
    replacement_text: str | None,
    target: RevisionTarget,
    draft_note: str | None = None,
) -> RevisionDraft:
    return RevisionDraft(
        revision_key=revision_key,
        finding_id=finding.finding_id,
        operation=operation,
        original_text=original_text,
        replacement_text=replacement_text,
        change_reason=finding.suggestion,
        target=target,
        revision_hash=compute_revision_hash(
            revision_key,
            operation,
            original_text,
            replacement_text,
            target,
        ),
        validation_status="VALID",
        draft_note=draft_note,
    )


def _unsupported(
    revision_key: str,
    finding: RevisionFindingSource,
    reason: str,
) -> RevisionDraft:
    return RevisionDraft(
        revision_key=revision_key,
        finding_id=finding.finding_id,
        operation="UNSUPPORTED",
        change_reason=finding.suggestion,
        unsupported_reason=reason,
        revision_hash=compute_revision_hash(revision_key, "UNSUPPORTED", None, None, None),
        validation_status="VALID",
    )


def _validate_source_identity(
    source: RevisionReviewSource,
    review_id: str,
    generation_id: str,
    result_hash: str,
) -> None:
    if source.review_status != "COMPLETED":
        raise RevisionDraftError("REVIEW_NOT_COMPLETED", "Review is not completed", status_code=409)
    if source.review_id != review_id:
        raise RevisionDraftError("REVIEW_NOT_FOUND", "Review identity mismatch", status_code=404)
    if source.generation_id != generation_id:
        raise RevisionDraftError("GENERATION_NOT_FOUND", "Generation identity mismatch", status_code=404)
    if source.result_hash != result_hash:
        raise RevisionDraftError("RESULT_HASH_MISMATCH", "Result hash mismatch", status_code=409)


async def _complete_revision_batch(
    runtime: Any,
    *,
    tenant_id: str,
    model_id: str,
    user_prompt: str,
) -> Any:
    system_prompt = (
        "你是合同条款修订器。风险已由上游正式确认；你只生成可直接替换原条款的中文文案。"
        "严格返回JSON对象，不使用工具，不输出解释性前后缀。"
    )
    messages = [{"role": "user", "content": user_prompt}]
    complete_with_usage = getattr(runtime, "complete_with_usage", None)
    if callable(complete_with_usage):
        return await complete_with_usage(
            messages=messages,
            model_id=model_id,
            system_prompt=system_prompt,
            max_tokens=4_000,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="revision_draft_mvp",
        )

    # Compatibility with an older isolated Framework runtime.  This keeps the
    # exact same configured model client and request controls; it is not a
    # parser fallback and does not weaken JSON validation.
    llm = await runtime.get_llm(model_id)
    from service.conversation.llm_runner import _build_thinking_extra_body

    response = await llm.client.chat.completions.create(
        model=llm.model,
        messages=[{"role": "system", "content": system_prompt}, *messages],
        stream=False,
        temperature=0,
        max_tokens=4_000,
        extra_body=_build_thinking_extra_body(llm, False),
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content if response.choices else ""
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = getattr(details, "cached_tokens", None)
    return SimpleNamespace(
        content=content or "",
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=getattr(usage, "completion_tokens", None),
    )


def _cache_key(review_id: str, generation_id: str, result_hash: str) -> str:
    return "\0".join((review_id, generation_id, result_hash))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _estimate_tokens(value: str) -> int:
    weighted = sum(1 if ord(char) > 127 else 0.25 for char in value)
    return max(1, int(weighted + 0.999))


def _extract_values(pattern: re.Pattern[str], value: str) -> set[str]:
    return {re.sub(r"\s+", "", item.group(0)) for item in pattern.finditer(value)}


def default_cache() -> FileRevisionDraftCache:
    root = Path(os.getenv("CONTRACT_REVISION_DRAFT_CACHE_DIR", ".contract-revision-drafts"))
    return FileRevisionDraftCache(root=root)
