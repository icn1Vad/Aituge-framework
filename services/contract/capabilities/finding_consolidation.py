"""Model-assisted, code-controlled duplicate Finding classification."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from service.conversation.llm_runner import LlmRuntime
from services.contract.capabilities.prompt_budget import evaluate_prompt_budget
from task_manager.output_parser import parse_json_output


REVIEW_ARTIFACT_TYPES = (
    "rights_obligations_review_result",
    "commercial_terms_review_result",
    "liability_termination_review_result",
    "missing_ambiguous_clauses_result",
    "relation_extraction_result",
)

_SYSTEM_PROMPT = """你是合同审查结果判重器。输入只包含代码筛出的候选 Finding 对。
你只能判断每一对的关系：
- SAME_RISK：同一合同风险的重复表达；
- RELATED_DISTINCT：有关联，但属于不同风险；
- DISTINCT：不是同一风险。
不得创建、删除或改写 Finding，不得修改风险等级、审查立场或证据。
SAME_RISK 必须同时具有相同的核心法律根因、对我方的主要后果和核心控制措施。
仅共享原文证据、属于同一履约链或标题相近，不足以构成 SAME_RISK。
例如，履行范围不封闭与变更程序缺失、服务标准不可衡量与验收程序缺失，
通常属于 RELATED_DISTINCT，除非输入明确证明二者实际上表达同一法律问题。
必须对输入中的每个 pair_id 恰好返回一次判断，不得遗漏或增加 pair_id。
只输出一个 JSON 对象，格式为 {"decisions":[{"pair_id":"...","relation":"SAME_RISK"}]}。
不要输出分析过程、解释、Markdown 或代码围栏。"""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelDecision(StrictModel):
    pair_id: str = Field(pattern=r"^pair-[0-9a-f]{32}$")
    relation: Literal["SAME_RISK", "RELATED_DISTINCT", "DISTINCT"]


class ModelDecisionEnvelope(StrictModel):
    decisions: list[ModelDecision]


class PromptBudgetHardLimitError(RuntimeError):
    """Provider input exceeded the frozen hard limit; this is not repairable."""


class LlmCompleter(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        system_prompt: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking_override: bool | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class FindingRef:
    artifact_type: str
    finding_id: str

    @property
    def key(self) -> tuple[str, str]:
        return self.artifact_type, self.finding_id

    def as_dict(self) -> dict[str, str]:
        return {"artifact_type": self.artifact_type, "finding_id": self.finding_id}


@dataclass(frozen=True, slots=True)
class CandidatePair:
    pair_id: str
    left: FindingRef
    right: FindingRef
    left_summary: dict[str, Any]
    right_summary: dict[str, Any]

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "left": self.left_summary,
            "right": self.right_summary,
        }


@dataclass(slots=True)
class FindingConsolidationEngine:
    runtime_factory: Callable[[str], LlmCompleter] = LlmRuntime
    max_pairs_per_call: int = 60
    max_estimated_prompt_tokens_per_call: int = 5_200

    async def consolidate(
        self,
        artifacts: dict[str, Any],
        *,
        tenant_id: str,
        model_id: str,
        finding_contexts: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run = await self.consolidate_with_metrics(
            artifacts,
            tenant_id=tenant_id,
            model_id=model_id,
            finding_contexts=finding_contexts,
        )
        return run["artifact"]

    async def consolidate_with_metrics(
        self,
        artifacts: dict[str, Any],
        *,
        tenant_id: str,
        model_id: str,
        finding_contexts: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        candidates = build_candidate_pairs(
            artifacts,
            finding_contexts=finding_contexts,
        )
        if not candidates:
            return {
                "artifact": _artifact(
                    "COMPLETED",
                    [],
                    candidate_count=0,
                    model_call_count=0,
                ),
                "wall_duration_ms": round((time.perf_counter() - started) * 1000),
                "call_metrics": [],
            }

        decisions: list[dict[str, Any]] = []
        call_count = 0
        call_metrics: list[dict[str, Any]] = []
        try:
            for batch in _candidate_batches(
                candidates,
                max_pairs=self.max_pairs_per_call,
                max_estimated_prompt_tokens=self.max_estimated_prompt_tokens_per_call,
            ):
                batch_decisions, attempts = await self._classify_batch(
                    batch,
                    tenant_id=tenant_id,
                    model_id=model_id,
                    call_metrics=call_metrics,
                )
                call_count += attempts
                decisions.extend(batch_decisions)
        except Exception:
            # Consolidation is an optional quality gate. A model or format failure
            # must never delete a valid Finding or fail the review task.
            return {
                "artifact": _artifact(
                    "SKIPPED",
                    [],
                    candidate_count=len(candidates),
                    model_call_count=len(call_metrics) or call_count,
                    skip_reason="MODEL_CLASSIFICATION_UNAVAILABLE",
                ),
                "wall_duration_ms": round((time.perf_counter() - started) * 1000),
                "call_metrics": call_metrics,
            }

        return {
            "artifact": _artifact(
                "COMPLETED",
                decisions,
                candidate_count=len(candidates),
                model_call_count=call_count,
            ),
            "wall_duration_ms": round((time.perf_counter() - started) * 1000),
            "call_metrics": call_metrics,
        }

    async def _classify_batch(
        self,
        candidates: list[CandidatePair],
        *,
        tenant_id: str,
        model_id: str,
        call_metrics: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        expected = {item.pair_id: item for item in candidates}
        payload = json.dumps(
            {"candidate_pairs": [item.prompt_payload() for item in candidates]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        feedback: str | None = None
        last_error: Exception | None = None
        runtime = self.runtime_factory(tenant_id)
        for attempt in (1, 2):
            prompt = f"请判断以下候选对：{payload}"
            if feedback:
                prompt += f"\n上次输出未通过结构校验：{feedback}。请完整重发全部判断。"
            try:
                completion = getattr(runtime, "complete_with_usage", None)
                if callable(completion):
                    batch_id = _consolidation_batch_id(candidates)
                    result = await completion(
                        messages=[{"role": "user", "content": prompt}],
                        model_id=model_id,
                        system_prompt=_SYSTEM_PROMPT,
                        max_tokens=max(800, min(8_000, 200 + len(candidates) * 80)),
                        temperature=0,
                        thinking_override=False,
                        response_format={"type": "json_object"},
                        review_unit_id="finding_consolidation",
                        repair_no=attempt - 1,
                    )
                    budget = evaluate_prompt_budget(
                        unit_id="finding_consolidation",
                        batch_id=batch_id,
                        estimated_business_context_tokens=max(1, round(len(payload) / 2)),
                        provider_prompt_tokens=result.prompt_tokens,
                        provider_cached_tokens=result.cached_tokens,
                    )
                    metric = {
                        "batch_id": batch_id,
                        "repair_no": attempt - 1,
                        "prompt_tokens": result.prompt_tokens,
                        "cached_tokens": result.cached_tokens,
                        "completion_tokens": result.completion_tokens,
                        "total_tokens": result.total_tokens,
                        "time_to_first_token_ms": result.time_to_first_token_ms,
                        "model_duration_ms": result.model_duration_ms,
                        "trace_id": result.trace_id,
                        "provider_request_id": result.provider_request_id,
                        "finish_reason": result.finish_reason,
                        "prompt_budget": budget.model_dump(mode="json"),
                    }
                    if call_metrics is not None:
                        call_metrics.append(metric)
                    if budget.budget_status == "HARD_LIMIT_EXCEEDED":
                        raise PromptBudgetHardLimitError(
                            "RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED"
                        )
                    content = result.content
                else:
                    content = await runtime.complete(
                        messages=[{"role": "user", "content": prompt}],
                        model_id=model_id,
                        system_prompt=_SYSTEM_PROMPT,
                        max_tokens=max(800, min(8_000, 200 + len(candidates) * 80)),
                        temperature=0,
                        thinking_override=False,
                    )
                    if call_metrics is not None:
                        call_metrics.append(
                            {
                                "batch_id": _consolidation_batch_id(candidates),
                                "repair_no": attempt - 1,
                                "prompt_tokens": None,
                                "cached_tokens": None,
                                "completion_tokens": None,
                                "total_tokens": None,
                                "time_to_first_token_ms": None,
                                "model_duration_ms": 0,
                                "trace_id": None,
                                "provider_request_id": None,
                                "finish_reason": None,
                                "prompt_budget": evaluate_prompt_budget(
                                    unit_id="finding_consolidation",
                                    batch_id=_consolidation_batch_id(candidates),
                                    estimated_business_context_tokens=max(
                                        1, round(len(payload) / 2)
                                    ),
                                    provider_prompt_tokens=None,
                                    provider_cached_tokens=None,
                                ).model_dump(mode="json"),
                            }
                        )
                envelope = _parse_model_output(content)
                actual_ids = [item.pair_id for item in envelope.decisions]
                if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
                    raise ValueError("pair_id coverage does not match the request")
                by_id = {item.pair_id: item.relation for item in envelope.decisions}
                return (
                    [
                        {
                            "pair_id": item.pair_id,
                            "left": item.left.as_dict(),
                            "right": item.right.as_dict(),
                            "relation": by_id[item.pair_id],
                        }
                        for item in candidates
                    ],
                    attempt,
                )
            except (ValueError, ValidationError) as exc:
                last_error = exc
                feedback = str(exc)[:300]
        assert last_error is not None
        raise last_error


def build_candidate_pairs(
    artifacts: dict[str, Any],
    *,
    finding_contexts: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[CandidatePair]:
    items: list[tuple[FindingRef, dict[str, Any], list[dict[str, Any]]]] = []
    for artifact_type in REVIEW_ARTIFACT_TYPES:
        artifact = artifacts.get(artifact_type)
        if not isinstance(artifact, dict):
            continue
        findings = artifact.get("findings")
        evidences = artifact.get("evidences")
        if not isinstance(findings, list) or not isinstance(evidences, list):
            continue
        evidence_by_finding: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for evidence in evidences:
            if isinstance(evidence, dict) and isinstance(evidence.get("finding_id"), str):
                evidence_by_finding[evidence["finding_id"]].append(evidence)
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("finding_id"), str):
                continue
            ref = FindingRef(artifact_type, finding["finding_id"])
            items.append((ref, finding, evidence_by_finding.get(ref.finding_id, [])))

    items.sort(key=lambda value: value[0].key)
    candidates: list[CandidatePair] = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if not _same_review_context(left[1], right[1]):
                continue
            if left[1].get("category") != right[1].get("category") and not _evidence_overlaps(
                left[2], right[2]
            ):
                continue
            pair_id = make_pair_id(left[0], right[0])
            candidates.append(
                CandidatePair(
                    pair_id=pair_id,
                    left=left[0],
                    right=right[0],
                    left_summary=_finding_summary_with_context(
                        left[0],
                        left[1],
                        left[2],
                        finding_contexts,
                    ),
                    right_summary=_finding_summary_with_context(
                        right[0],
                        right[1],
                        right[2],
                        finding_contexts,
                    ),
                )
            )
    return candidates


def _finding_summary_with_context(
    ref: FindingRef,
    finding: dict[str, Any],
    evidences: list[dict[str, Any]],
    contexts: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    summary = _finding_summary(finding, evidences)
    if contexts is not None:
        context = contexts.get(ref.key)
        if context:
            summary["compatibility_context"] = dict(context)
    return summary


def make_pair_id(left: FindingRef, right: FindingRef) -> str:
    ordered = sorted((left.key, right.key))
    canonical = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return "pair-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _consolidation_batch_id(candidates: list[CandidatePair]) -> str:
    canonical = json.dumps(
        [item.pair_id for item in candidates],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "risk-batch-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _candidate_batches(
    candidates: list[CandidatePair],
    *,
    max_pairs: int,
    max_estimated_prompt_tokens: int,
) -> list[list[CandidatePair]]:
    """Pack ordered pairs without truncation using a conservative local budget.

    The Provider usage remains authoritative after the call.  This estimate only
    prevents an obviously oversized request from reaching the Provider.
    """

    if max_pairs < 1 or max_estimated_prompt_tokens < 1:
        raise ValueError("Finding consolidation batch limits must be positive")
    batches: list[list[CandidatePair]] = []
    current: list[CandidatePair] = []
    for candidate in candidates:
        proposed = [*current, candidate]
        if current and (
            len(proposed) > max_pairs
            or _estimated_prompt_tokens(proposed) > max_estimated_prompt_tokens
        ):
            batches.append(current)
            current = [candidate]
        else:
            current = proposed
    if current:
        batches.append(current)
    return batches


def _estimated_prompt_tokens(candidates: list[CandidatePair]) -> int:
    payload = json.dumps(
        {"candidate_pairs": [item.prompt_payload() for item in candidates]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = f"请判断以下候选对：{payload}"
    # This is intentionally conservative for the mixed Chinese/JSON payload.
    return max(1, round((len(_SYSTEM_PROMPT) + len(prompt)) / 2))


def _finding_summary(finding: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "category": finding.get("category"),
        "risk_level": finding.get("risk_level"),
        "title": str(finding.get("title") or "")[:300],
        "issue": str(finding.get("issue") or "")[:800],
        "evidence": [_evidence_summary(item) for item in evidences[:8]],
    }


def _evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    if evidence.get("evidence_type") == "ABSENCE":
        return {
            "evidence_type": "ABSENCE",
            "checked_scope": str(evidence.get("checked_scope") or "")[:300],
        }
    return {
        "evidence_type": evidence.get("evidence_type"),
        "block_id": evidence.get("block_id"),
        "char_start": evidence.get("char_start"),
        "char_end": evidence.get("char_end"),
        "quoted_text": str(evidence.get("quoted_text") or "")[:500] or None,
    }


def _same_review_context(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("perspective") == right.get("perspective")
        and _normalize(left.get("our_party")) == _normalize(right.get("our_party"))
        and _normalize(left.get("counterparty")) == _normalize(right.get("counterparty"))
    )


def _evidence_overlaps(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    for first in left:
        for second in right:
            if first.get("evidence_type") == "ABSENCE" and second.get("evidence_type") == "ABSENCE":
                scope = _normalize(first.get("checked_scope"))
                if scope and scope == _normalize(second.get("checked_scope")):
                    return True
                continue
            if not first.get("block_id") or first.get("block_id") != second.get("block_id"):
                continue
            starts = (first.get("char_start"), second.get("char_start"))
            ends = (first.get("char_end"), second.get("char_end"))
            if all(isinstance(value, int) for value in (*starts, *ends)):
                if max(starts) < min(ends):
                    return True
    return False


def _normalize(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _parse_model_output(content: str) -> ModelDecisionEnvelope:
    parsed = parse_json_output(content)
    if not parsed.ok or not isinstance(parsed.structured, dict):
        raise ValueError("model did not return one complete JSON object")
    return ModelDecisionEnvelope.model_validate(parsed.structured)


def _artifact(
    status: Literal["COMPLETED", "SKIPPED"],
    decisions: list[dict[str, Any]],
    *,
    candidate_count: int,
    model_call_count: int,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "result_type": "FINDING_CONSOLIDATION_V1",
        "status": status,
        "candidate_count": candidate_count,
        "model_call_count": model_call_count,
        "decisions": decisions,
        "skip_reason": skip_reason,
    }
