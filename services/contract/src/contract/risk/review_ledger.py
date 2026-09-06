"""Source-bound task scopes and review records, independent of checklist size.

Scope completeness means all evidence selected by the frozen IR projection, not
proof that OCR/IR captured every fact in the original document.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckTaskScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    scope_basis: Literal["FROZEN_POLICY_EVIDENCE_PROJECTION"] = "FROZEN_POLICY_EVIDENCE_PROJECTION"
    expected_item_ids: list[str] = Field(default_factory=list)
    provided_item_ids: list[str] = Field(default_factory=list)
    expected_anchor_ids: list[str] = Field(default_factory=list)
    provided_anchor_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_subset(self) -> "CheckTaskScope":
        for expected, provided in (
            (self.expected_item_ids, self.provided_item_ids),
            (self.expected_anchor_ids, self.provided_anchor_ids),
        ):
            if len(expected) != len(set(expected)) or len(provided) != len(set(provided)):
                raise ValueError("Task scope IDs must be unique")
            if not set(provided).issubset(expected):
                raise ValueError("Task evidence must belong to its frozen scope")
        return self

    @property
    def complete(self) -> bool:
        return (set(self.expected_item_ids) == set(self.provided_item_ids)
                and set(self.expected_anchor_ids) == set(self.provided_anchor_ids))

    def prompt_record(self) -> dict[str, Any]:
        return {
            "check_code": self.check_code,
            "scope_basis": self.scope_basis,
            "scope_complete": self.complete,
            "expected_source_count": len(self.expected_anchor_ids),
            "provided_source_count": len(self.provided_anchor_ids),
            "instruction": (
                "记录所提供证据的判断依据；投影完整不等于OCR、IR或合同附件完整。"
                if self.complete else
                "仅记录本分片可证实的问题；禁止从局部未见推断整份合同缺失或全局不适用；未决事项记录为证据不足。"
            ),
        }


class CheckReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_version: Literal["1.0"] = "1.0"
    scope_basis: Literal["FROZEN_POLICY_EVIDENCE_PROJECTION", "FROZEN_HORIZONTAL_CANDIDATES"] = "FROZEN_POLICY_EVIDENCE_PROJECTION"
    review_id: str
    generation_id: str
    plan_id: str
    unit_id: str
    check_code: str
    review_question: str
    batch_ids: list[str]
    context_hashes: list[str]
    execution_status: Literal["COMPLETED", "PARTIAL", "FAILED", "NOT_RUN"]
    judgement: Literal["RISK", "NO_RISK", "NOT_APPLICABLE", "INSUFFICIENT_EVIDENCE", "UNRESOLVED"]
    scope_complete: bool
    expected_anchor_ids: list[str]
    provided_anchor_ids: list[str]
    cited_anchor_ids: list[str]
    finding_local_ids: list[str]
    decision_notes: list[str]
    unresolved_reasons: list[str]
    model_call_count: int = Field(ge=0)
    # Costs are batch-owned. Never sum them per check when one call serves many.
    cost_scope: Literal["SHARED_BATCH_METRICS"] = "SHARED_BATCH_METRICS"


def build_review_records(plan: Any, batches: list[Any]) -> list[CheckReviewRecord]:
    """Keep successful findings AND unresolved sub-tasks; never infer no risk.

    This runs on stored outputs and does not call a model. Missing/legacy scope
    metadata is explicitly unknown, rather than fabricated full coverage.
    """
    by_batch = {batch.batch_id: batch for batch in batches}
    records = []
    for unit in plan.review_units:
        contexts = [context for context in plan.contexts if context.unit_id == unit.unit_id]
        if not contexts or not any(c.batch_id in by_batch for c in contexts):
            continue
        for spec in unit.check_specs:
            assigned = [c for c in contexts if any(s.check_code == spec.check_code for s in c.check_specs)]
            checks, scopes, findings = [], [], []
            missing = []
            for context in assigned:
                scope = next((s for s in context.check_task_scopes if s.check_code == spec.check_code), None)
                if scope is not None:
                    scopes.append(scope)
                else:
                    missing.append("SCOPE_METADATA_MISSING")
                batch = by_batch.get(context.batch_id)
                check = next((c for c in batch.check_results if c.check_code == spec.check_code), None) if batch else None
                if check is None:
                    missing.append("ASSIGNED_TASK_NOT_RETURNED")
                else:
                    checks.append(check)
                    findings.extend(f for f in batch.findings if f.check_code == spec.check_code)
            expected = sorted({a for scope in scopes for a in scope.expected_anchor_ids})
            provided = sorted({a for scope in scopes for a in scope.provided_anchor_ids})
            complete = bool(scopes) and len(scopes) == len(assigned) and set(expected) == set(provided)
            # All shards having been sent is not the same as a joint decision.
            jointly_reviewed = any(scope.complete for scope in scopes)
            failed = any(c.status == "FAILED" for c in checks)
            insufficient = any(c.reason_code == "INSUFFICIENT_EVIDENCE" for c in checks)
            if failed:
                missing.append("CHECK_EXECUTION_FAILED")
            if insufficient:
                missing.append("INSUFFICIENT_EVIDENCE")
            if not complete:
                missing.append("SOURCE_SCOPE_INCOMPLETE")
            if not jointly_reviewed:
                missing.append("CROSS_SHARD_SYNTHESIS_REQUIRED")
            ids = sorted({f.finding_local_id for f in findings})
            if ids:
                judgement = "RISK"
            elif missing:
                judgement = "INSUFFICIENT_EVIDENCE" if insufficient or not complete or not jointly_reviewed else "UNRESOLVED"
            elif checks and all(c.status == "NOT_APPLICABLE" for c in checks):
                judgement = "NOT_APPLICABLE"
            elif checks and all(c.status == "REVIEWED" and c.reason_code == "NO_RISK_IDENTIFIED" for c in checks):
                judgement = "NO_RISK"
            else:
                judgement = "UNRESOLVED"
                missing.append("JUDGEMENT_NOT_RESOLVED")
            records.append(CheckReviewRecord(
                review_id=plan.review_id, generation_id=plan.generation_id,
                plan_id=plan.plan_id, unit_id=unit.unit_id, check_code=spec.check_code,
                review_question=spec.review_question,
                batch_ids=[c.batch_id for c in assigned],
                context_hashes=[c.context_hash for c in assigned],
                execution_status=("NOT_RUN" if not checks else "FAILED" if all(c.status == "FAILED" for c in checks)
                                  else "PARTIAL" if failed or len(checks) != len(assigned) else "COMPLETED"),
                judgement=judgement, scope_complete=complete,
                expected_anchor_ids=expected, provided_anchor_ids=provided,
                cited_anchor_ids=sorted({e.anchor_id for f in findings for e in f.evidence_candidates if e.anchor_id}),
                finding_local_ids=ids,
                decision_notes=list(dict.fromkeys(c.decision_note for c in checks)),
                unresolved_reasons=sorted(set(missing)),
                model_call_count=sum(by_batch[c.batch_id].model_call_count for c in assigned if c.batch_id in by_batch),
            ))
    return records
