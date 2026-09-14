import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.rule_lab import DEMO_RULES, DEMO_FRAGMENTS, DemoRuntime, Run, demo_cases, digest, execute
from backend.rule_lab_trial import TrialRequest, trial, trial_perspective


def snapshot():
    return dict(rules=DEMO_RULES, tenant_id="42", source_version="test", snapshot_id="test-snapshot", kind="LOCAL_TEST_LIBRARY",
        snapshot_hash=digest([r.model_dump(mode="json") for r in DEMO_RULES]))


class Runtime(DemoRuntime):
    def __init__(self):
        self.calls = []

    async def complete_with_usage(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("review_unit_id") == "rule_trial_sample":
            return SimpleNamespace(content=json.dumps({"fragment": DEMO_FRAGMENTS[0][1]}), prompt_tokens=30, completion_tokens=20)
        return await super().complete_with_usage(**kwargs)


def test_no_expectation_returns_observation_not_test_pass():
    case = demo_cases()[0].model_copy(update={"expected": []})
    result = asyncio.run(execute(Run(run_id="no-oracle-001", snapshot_id="s", mode="LIVE", case=case), snapshot(), runtime=Runtime()))
    assert result["verdict"] == "OBSERVED" and result["assertions"] == []
    assert result["review"]["decisions"]


def test_rule_only_auto_assigns_generates_fragment_and_reviews_once(tmp_path):
    runtime = Runtime()
    value = TrialRequest(run_id="simple-trial-001", rule_id=DEMO_RULES[0].rule_id)
    result = asyncio.run(trial(value, snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model"))
    assert result["status"] == "REVIEWED" and result["rule_applied"]
    assert result["source_kind"] == "AI_DEMONSTRATION"
    assert result["execution"]["case"]["expected"] == []
    assert result["execution"]["verdict"] == "OBSERVED"
    assert {x["code"] for x in result["assigned_checks"]} == {"CF-002", "CF-005"}
    assert len(runtime.calls) == 2
    assert all('"expected"' not in call["messages"][0]["content"] for call in runtime.calls)
    assert asyncio.run(trial(value, snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model")) == result
    assert len(runtime.calls) == 2


def test_user_fragment_never_generated_or_rewritten(tmp_path):
    runtime = Runtime()
    value = TrialRequest(run_id="user-fragment-001", rule_id=DEMO_RULES[0].rule_id, fragment=DEMO_FRAGMENTS[0][1])
    result = asyncio.run(trial(value, snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model"))
    assert result["rule_applied"] and result["source_kind"] == "USER_FRAGMENT"
    assert result["fragment"] == value.fragment and result["sample_usage"]["model_calls"] == 0
    assert len(runtime.calls) == 1
    with pytest.raises(ValueError, match="不能更换输入"):
        asyncio.run(trial(value.model_copy(update={"fragment": "different"}), snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model"))


def test_unknown_assignment_does_not_force_rule_into_reviewer(tmp_path):
    runtime = Runtime()
    rule = DEMO_RULES[0].model_copy(update={"name": "特殊要求", "review_direction": "特殊约定"})
    result = asyncio.run(trial(TrialRequest(run_id="unknown-check-001", rule_id=rule.rule_id), snapshot(), rule, runtime, tmp_path, "test-model"))
    assert result["status"] == "NOT_ASSIGNED" and not result["rule_applied"]
    assert runtime.calls == []


def test_business_role_is_not_assumed_party_a(tmp_path):
    assert trial_perspective(DEMO_RULES[0], "乙方（买受方）负责预付款") == "PARTY_B"
    assert trial_perspective(DEMO_RULES[0], "甲方负责付款") is None
    runtime = Runtime()
    result = asyncio.run(trial(TrialRequest(run_id="missing-party-001", rule_id=DEMO_RULES[0].rule_id, fragment="甲方负责付款"),
        snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model"))
    assert result["status"] == "NEEDS_CONTEXT" and runtime.calls == []


def test_failed_sample_has_no_hidden_retry_and_preserves_saved_rule(tmp_path):
    class Broken(Runtime):
        async def complete_with_usage(self, **kwargs):
            self.calls.append(kwargs)
            raise TimeoutError()
    runtime = Broken()
    value = TrialRequest(run_id="sample-timeout-001", rule_id=DEMO_RULES[0].rule_id)
    result = asyncio.run(trial(value, snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model"))
    assert result["status"] == "INCOMPLETE" and result["rule"]["rule_id"] == DEMO_RULES[0].rule_id
    assert not result["rule_applied"] and len(runtime.calls) == 1
    assert result["sample_usage"]["model_calls"] == 1
    assert asyncio.run(trial(value, snapshot(), DEMO_RULES[0], runtime, tmp_path, "test-model")) == result
    assert len(runtime.calls) == 1
