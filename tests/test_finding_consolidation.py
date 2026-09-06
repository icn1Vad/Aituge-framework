from __future__ import annotations

import asyncio
import json

from service.conversation.llm_runner import LlmCompletionResult
from services.contract.capabilities.finding_consolidation import (
    FindingConsolidationEngine,
    _candidate_batches,
    build_candidate_pairs,
)


class FakeRuntime:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.responses.pop(0)

    async def complete_with_usage(self, messages, **kwargs):
        # The production reviewer uses the usage-bearing protocol, including
        # when testing malformed JSON and its one permitted repair.
        self.calls.append({"messages": messages, **kwargs})
        return await FakeUsageRuntime(self.responses.pop(0), 1200).complete_with_usage(
            messages, **kwargs
        )


class FakeUsageRuntime:
    def __init__(self, response: str, prompt_tokens: int) -> None:
        self.response = response
        self.prompt_tokens = prompt_tokens
        self.calls: list[dict] = []

    async def complete_with_usage(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return LlmCompletionResult(
            content=self.response,
            prompt_tokens=self.prompt_tokens,
            cached_tokens=100,
            completion_tokens=20,
            total_tokens=self.prompt_tokens + 20,
            time_to_first_token_ms=10,
            model_duration_ms=20,
            trace_id="trace-test",
            provider_request_id="request-test",
            finish_reason="stop",
            review_unit_id="finding_consolidation",
            review_id=None,
            framework_run_id=None,
            attempt_no=None,
            repair_no=kwargs["repair_no"],
        )


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


def test_usage_metrics_use_provider_tokens_and_do_not_double_count_cache() -> None:
    artifacts = _artifacts()
    candidate = build_candidate_pairs(artifacts)[0]
    runtime = FakeUsageRuntime(
        json.dumps(
            {
                "decisions": [
                    {"pair_id": candidate.pair_id, "relation": "DISTINCT"}
                ]
            }
        ),
        prompt_tokens=6_145,
    )
    engine = FindingConsolidationEngine(runtime_factory=lambda _tenant: runtime)

    run = asyncio.run(
        engine.consolidate_with_metrics(
            artifacts,
            tenant_id="tenant-1",
            model_id="contract-model",
        )
    )

    assert run["artifact"]["status"] == "COMPLETED"
    assert len(runtime.calls) == 1
    metric = run["call_metrics"][0]
    assert metric["prompt_tokens"] == 6_145
    assert metric["cached_tokens"] == 100
    assert metric["prompt_budget"]["provider_prompt_tokens"] == 6_145
    assert metric["prompt_budget"]["budget_status"] == "SOFT_WARNING"


def test_large_provider_prompt_preserves_valid_consolidation_without_repair() -> None:
    artifacts = _artifacts()
    candidate = build_candidate_pairs(artifacts)[0]
    runtime = FakeUsageRuntime(
        json.dumps(
            {
                "decisions": [
                    {"pair_id": candidate.pair_id, "relation": "DISTINCT"}
                ]
            }
        ),
        prompt_tokens=17_694,
    )
    engine = FindingConsolidationEngine(runtime_factory=lambda _tenant: runtime)

    run = asyncio.run(
        engine.consolidate_with_metrics(
            artifacts,
            tenant_id="tenant-1",
            model_id="contract-model",
        )
    )

    assert run["artifact"]["status"] == "COMPLETED"
    assert run["artifact"]["model_call_count"] == 1
    assert len(runtime.calls) == 1
    assert run["call_metrics"][0]["prompt_budget"]["budget_status"] == (
        "SOFT_WARNING"
    )


def test_candidate_batches_are_deterministic_and_never_drop_pairs() -> None:
    candidates = build_candidate_pairs(_many_artifacts())

    first = _candidate_batches(
        candidates,
        max_pairs=60,
        max_estimated_prompt_tokens=400,
    )
    second = _candidate_batches(
        candidates,
        max_pairs=60,
        max_estimated_prompt_tokens=400,
    )

    assert [[item.pair_id for item in batch] for batch in first] == [
        [item.pair_id for item in batch] for batch in second
    ]
    assert [item.pair_id for batch in first for item in batch] == [
        item.pair_id for item in candidates
    ]
    assert len(first) > 1


def test_internal_compatibility_context_is_visible_without_changing_pair_id() -> None:
    artifacts = _artifacts()
    baseline = build_candidate_pairs(artifacts)[0]
    contexts = {
        baseline.left.key: {
            "source_check_code": "CF-008",
            "risk_type": "ACCEPTANCE_RISK",
        },
        baseline.right.key: {
            "source_check_code": "PO-004",
            "risk_type": "SERVICE_LEVEL_RISK",
        },
    }

    enriched = build_candidate_pairs(
        artifacts,
        finding_contexts=contexts,
    )[0]

    assert enriched.pair_id == baseline.pair_id
    assert enriched.left_summary["compatibility_context"] == contexts[
        enriched.left.key
    ]
    assert enriched.right_summary["compatibility_context"] == contexts[
        enriched.right.key
    ]


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


def _many_artifacts() -> dict:
    findings = [
        _finding(f"finding-{index}", f"Acceptance risk {index}")
        for index in range(5)
    ]
    evidence = [
        _evidence(f"evidence-{index}", f"finding-{index}")
        for index in range(5)
    ]
    return {
        "commercial_terms_review_result": {
            "findings": findings,
            "evidences": evidence,
        }
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
