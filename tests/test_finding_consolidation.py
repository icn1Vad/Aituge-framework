from __future__ import annotations

import asyncio
import json

from services.contract.capabilities.finding_consolidation import (
    FindingConsolidationEngine,
    build_candidate_pairs,
)


class FakeRuntime:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.responses.pop(0)


def test_builds_candidate_pairs_for_same_category_and_overlapping_evidence() -> None:
    artifacts = _artifacts()

    candidates = build_candidate_pairs(artifacts)

    assert len(candidates) == 1
    assert candidates[0].left.artifact_type == "commercial_terms_review_result"
    assert candidates[0].right.artifact_type == "rights_obligations_review_result"
    assert candidates[0].left_summary["title"] == "Acceptance procedure is absent"
    assert candidates[0].right_summary["title"] == "No acceptance standard"


def test_classifies_all_candidates_in_one_non_thinking_call() -> None:
    artifacts = _artifacts()
    candidate = build_candidate_pairs(artifacts)[0]
    runtime = FakeRuntime(
        [
            json.dumps(
                {
                    "decisions": [
                        {"pair_id": candidate.pair_id, "relation": "SAME_RISK"}
                    ]
                }
            )
        ]
    )
    engine = FindingConsolidationEngine(runtime_factory=lambda _tenant: runtime)

    result = asyncio.run(
        engine.consolidate(artifacts, tenant_id="tenant-1", model_id="contract-model")
    )

    assert result["status"] == "COMPLETED"
    assert result["candidate_count"] == 1
    assert result["model_call_count"] == 1
    assert result["decisions"][0]["relation"] == "SAME_RISK"
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["thinking_override"] is False
    assert runtime.calls[0]["temperature"] == 0


def test_repairs_invalid_coverage_once_then_succeeds() -> None:
    artifacts = _artifacts()
    candidate = build_candidate_pairs(artifacts)[0]
    runtime = FakeRuntime(
        [
            '{"decisions":[]}',
            json.dumps(
                {
                    "decisions": [
                        {"pair_id": candidate.pair_id, "relation": "DISTINCT"}
                    ]
                }
            ),
        ]
    )
    engine = FindingConsolidationEngine(runtime_factory=lambda _tenant: runtime)

    result = asyncio.run(
        engine.consolidate(artifacts, tenant_id="tenant-1", model_id="contract-model")
    )

    assert result["status"] == "COMPLETED"
    assert result["model_call_count"] == 2
    assert len(runtime.calls) == 2
    assert "上次输出未通过结构校验" in runtime.calls[1]["messages"][0]["content"]


def test_preserves_all_findings_when_model_output_stays_invalid() -> None:
    artifacts = _artifacts()
    runtime = FakeRuntime(['{"decisions":[]}', '{"decisions":[]}'])
    engine = FindingConsolidationEngine(runtime_factory=lambda _tenant: runtime)

    result = asyncio.run(
        engine.consolidate(artifacts, tenant_id="tenant-1", model_id="contract-model")
    )

    assert result["status"] == "SKIPPED"
    assert result["decisions"] == []
    assert result["skip_reason"] == "MODEL_CLASSIFICATION_UNAVAILABLE"
    assert len(runtime.calls) == 2


def _artifacts() -> dict:
    first = _finding("finding-a", "No acceptance standard")
    second = _finding("finding-b", "Acceptance procedure is absent")
    return {
        "rights_obligations_review_result": {
            "findings": [first],
            "evidences": [_evidence("evidence-a", "finding-a")],
        },
        "commercial_terms_review_result": {
            "findings": [second],
            "evidences": [_evidence("evidence-b", "finding-b")],
        },
    }


def _finding(finding_id: str, title: str) -> dict:
    return {
        "finding_id": finding_id,
        "category": "ACCEPTANCE",
        "risk_level": "MEDIUM",
        "title": title,
        "perspective": "PARTY_A",
        "our_party": "Acme",
        "counterparty": "Beta",
        "issue": "The contract lacks a complete acceptance mechanism.",
    }


def _evidence(evidence_id: str, finding_id: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "finding_id": finding_id,
        "evidence_type": "TEXT_QUOTE",
        "block_id": "block-1",
        "char_start": 10,
        "char_end": 30,
        "quoted_text": "acceptance source text",
    }
