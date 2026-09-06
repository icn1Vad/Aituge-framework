import asyncio
from dataclasses import replace
import pytest
from contract.api.models import CreateReviewRequest
from contract.application.idempotency import build_request_fingerprint
from contract.application.framework_gateway import FrameworkExecutionRequest
from contract.rule_evidence.execution import RuleLibraryExecution
from risk_test_data import risk_plan_input
from test_rule_library_shadow import snapshot
from test_rule_library_reviewer import FakeModel


def test_task_standard_is_validated_and_part_of_both_fingerprints():
    base = dict(business_task_id="b1", contract_version_id="c1", perspective="PARTY_A",
                contract_type="AUTO", review_attitude="NEUTRAL", schema_version="1.0")
    request = CreateReviewRequest(**base)
    assert request.rule_review_standard == "neutral"
    hashes = set()
    for standard in ("neutral", "strong", "weak"):
        value = CreateReviewRequest(**base, rule_review_standard=standard)
        assert value.review_attitude == "NEUTRAL"
        hashes.add(build_request_fingerprint(tenant_id="42", user_id="1", request=value,
                                            file_sha256="sha256:" + "0" * 64)[0])
    assert len(hashes) == 3
    with pytest.raises(ValueError):
        CreateReviewRequest(**base, rule_review_standard="anything")
    framework = FrameworkExecutionRequest(review_id="r1", attempt_no=1, tenant_id="42", user_id="1",
        document_id="d1", our_party_name="甲方", **base)
    assert len({replace(framework, rule_review_standard=s).request_fingerprint
                for s in ("neutral", "strong", "weak")}) == 3


def test_concurrent_standards_select_correct_rules_and_never_change_legal_plan(tmp_path):
    value = risk_plan_input()
    before = value.model_dump_json()
    model = FakeModel()
    execution = RuleLibraryExecution(snapshot(tmp_path), tmp_path / "cache",
                                    runtime_factory=lambda tenant: model)
    async def run():
        return await asyncio.gather(*(execution.run(value, "42", "fake", standard=s,
            contract_type_name="采购合同", business_role="买受方") for s in ("neutral", "strong", "weak")))
    results = asyncio.run(run())
    for standard, result in zip(("neutral", "strong", "weak"), results):
        assert result.review_standard == standard
        assert result.status == "COMPLETED"
        assert result.evidence and all(e.rule_id == standard for e in result.evidence)
        assert result.decisions
    assert len({r.bundle_hash for r in results}) == 3
    calls = model.calls
    assert asyncio.run(run()) == results
    assert model.calls == calls == 3
    assert execution.standard == "neutral"
    assert value.model_dump_json() == before
    with pytest.raises(ValueError):
        asyncio.run(execution.run(value, "42", "fake", standard="invalid"))
