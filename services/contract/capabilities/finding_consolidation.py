"""Model-assisted, code-controlled duplicate Finding classification."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from service.conversation.llm_runner import LlmRuntime
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

    async def consolidate(
        self,
        artifacts: dict[str, Any],
        *,
        tenant_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        candidates = build_candidate_pairs(artifacts)
        if not candidates:
            return _artifact("COMPLETED", [], candidate_count=0, model_call_count=0)

        decisions: list[dict[str, Any]] = []
        call_count = 0
        try:
            for start in range(0, len(candidates), self.max_pairs_per_call):
                batch = candidates[start : start + self.max_pairs_per_call]
                batch_decisions, attempts = await self._classify_batch(
                    batch,
                    tenant_id=tenant_id,
                    model_id=model_id,
                )
                call_count += attempts
                decisions.extend(batch_decisions)
        except Exception:
            # Consolidation is an optional quality gate. A model or format failure
            # must never delete a valid Finding or fail the review task.
            return _artifact(
                "SKIPPED",
                [],
                candidate_count=len(candidates),
                model_call_count=call_count,
                skip_reason="MODEL_CLASSIFICATION_UNAVAILABLE",
            )

        return _artifact(
            "COMPLETED",
            decisions,
            candidate_count=len(candidates),
            model_call_count=call_count,
        )

    async def _classify_batch(
        self,
        candidates: list[CandidatePair],
        *,
        tenant_id: str,
        model_id: str,
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
                content = await runtime.complete(
                    messages=[{"role": "user", "content": prompt}],
                    model_id=model_id,
                    system_prompt=_SYSTEM_PROMPT,
                    max_tokens=max(800, min(8_000, 200 + len(candidates) * 80)),
                    temperature=0,
                    thinking_override=False,
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


def build_candidate_pairs(artifacts: dict[str, Any]) -> list[CandidatePair]:
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
                    left_summary=_finding_summary(left[1], left[2]),
                    right_summary=_finding_summary(right[1], right[2]),
                )
            )
    return candidates


def make_pair_id(left: FindingRef, right: FindingRef) -> str:
    ordered = sorted((left.key, right.key))
    canonical = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return "pair-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


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
