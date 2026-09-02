from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from contract.api.models import ContractProfile
from contract.callback.models import FindingConsolidationArtifact
from services.contract.capabilities.direct_e2e import (
    DirectE2EError,
    DirectE2EStageMetric,
    DirectRiskReviewEndToEndRequest,
    DirectRiskReviewEndToEndRunner,
    DryRunResultSink,
    build_final_callback,
    build_formal_result,
    build_success_state_trace,
    core_result_signature,
    frozen_input_snapshot,
    stable_hash,
)
from services.contract.capabilities.legacy_compatibility import (
    LegacyCompatibilityContext,
    LegacyRiskArtifactAdapter,
    finalize_legacy_compatible_result,
)
from services.contract.scripts.contract_risk_stage66_direct_e2e import (
    _core_components,
)


BLOCK_TEXT = "Party A must prepay the full price. Party B has unlimited liability."
BLOCK = {
    "block_id": "block-1",
    "block_no": 1,
    "page_number": None,
    "text": BLOCK_TEXT,
}


def test_core_signature_records_missing_fva_assessment_without_crashing() -> None:
    fva = SimpleNamespace(
        unit_id="formation_validity_authority",
        check_results=[
            SimpleNamespace(
                check_code="FVA-002",
                reason_code="INSUFFICIENT_EVIDENCE",
                finding_local_ids=[],
            )
        ],
        fva_assessments=[],
    )
    empty_unit = lambda unit_id: SimpleNamespace(
        unit_id=unit_id,
        findings=[],
        canonical_risk_roots=[],
    )
    extended = SimpleNamespace(
        base_bundle=SimpleNamespace(
            units=[
                fva,
                empty_unit("commercial_financial"),
                empty_unit("performance_obligations"),
                empty_unit("ip_confidentiality_data"),
                empty_unit("liability_remedies_exit"),
            ]
        ),
        horizontal_units=[],
    )

    assert _core_components(extended) == [
        {
            "check_code": "FVA-002",
            "assessment_type": "UNAVAILABLE",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "finding_count": 0,
        }
    ]


def test_frozen_input_hash_is_stable_and_snapshots_are_isolated() -> None:
    source_ir = {"semantic_ir": {"obligations": [{"item_id": "I001"}]}}
    source_context = {"perspective": "PARTY_A"}
    hashes = set()
    for _ in range(100):
        ir, context, digest = frozen_input_snapshot(source_ir, source_context)
        hashes.add(digest)
        ir["semantic_ir"]["obligations"].append({"item_id": "I002"})
        context["perspective"] = "PARTY_B"
    assert len(hashes) == 1
    assert source_ir["semantic_ir"]["obligations"] == [{"item_id": "I001"}]
    assert source_context["perspective"] == "PARTY_A"


def test_only_dry_run_execution_mode_is_accepted() -> None:
    with pytest.raises(ValidationError):
        _request(execution_mode="LIVE")


def test_formal_payload_reuses_frozen_dto_and_result_hash() -> None:
    compatible = _compatible()
    formal, payload, payload_hash = build_formal_result(
        compatible,
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    assert formal.result_type == "FINAL_REVIEW_STAGE_V1"
    assert payload.schema_version == "1.0"
    assert payload.result_hash.startswith("sha256:")
    assert payload_hash == stable_hash(formal.model_dump(mode="json"))
    assert len(payload.findings) == 1
    assert len(payload.evidences) == 1


def test_formal_payload_embeds_every_cited_legal_source() -> None:
    legal_id = "legal-evidence-" + "2" * 32
    bundle = SimpleNamespace(
        usable=True,
        release_id="release-legal-test",
        bundle_hash="sha256:" + "b" * 64,
        version_snapshot={
            "legal_release_id": "release-legal-test",
            "legal_projection_version": "legal-evidence-projection-v4",
            "relation_extractor_version": "legal-relation-extractor-v2",
            "embedding_model_version": "embedding-test-v1",
            "reranker_version": "reranker-test-v1",
            "planner_version": "adaptive-legal-planner-v2",
            "review_as_of_date": "2026-09-02",
            "contract_date": "2026-08-01",
        },
        relation_paths=[],
        applicability_decisions=[
            SimpleNamespace(
                evidence_id=legal_id,
                unit_id="unit-legal-test",
                issue_id="legal-issue-" + "3" * 32,
                outcome="UNKNOWN_METADATA",
                jurisdiction_decision="MATCH",
                temporal_decision="UNKNOWN",
                review_as_of_date="2026-09-02",
                contract_date="2026-08-01",
                reasons=["法规时效元数据尚未核验"],
            )
        ],
        evidence=[
            SimpleNamespace(
                evidence_id=legal_id,
                    check_codes=["CF-005"],
                    issue_ids=["legal-issue-" + "3" * 32],
                    retrieval_channels=["KEYWORD", "VECTOR"],
                    relevance_score=0.92,
                    rerank_score=0.95,
                cautions=["METADATA_UNVERIFIED"],
                unit=SimpleNamespace(
                    release_id="release-legal-test",
                    unit_id="unit-legal-test",
                    instrument_id="instrument-legal-test",
                    version_id="version-legal-test",
                    source_node_ids=["node-legal-test"],
                    title="中华人民共和国民法典",
                    article_no="第五百零九条",
                    heading_path=["第三编 合同"],
                    content="当事人应当按照约定全面履行自己的义务。",
                    jurisdiction="CN",
                    authority_level="LAW",
                    issuing_authority="全国人民代表大会",
                    effective_from=None,
                    effective_to=None,
                    validity_status=None,
                    metadata_verification_status="UNVERIFIED",
                    official_source_url=None,
                    content_hash="4" * 64,
                ),
            )
        ],
    )
    _formal, payload, _payload_hash = build_formal_result(
        _compatible(legal_evidence_ids=[legal_id]),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
        legal_evidence_bundle=bundle,
    )

    assert payload.legal_evidence_release_id == "release-legal-test"
    assert payload.legal_evidence_bundle_hash == "sha256:" + "b" * 64
    assert [item.evidence_id for item in payload.legal_evidences] == [legal_id]
    assert payload.legal_evidences[0].metadata_verification_status == "UNVERIFIED"


def test_formal_payload_rejects_dangling_legal_source_id() -> None:
    with pytest.raises(DirectE2EError, match="usable frozen bundle"):
        build_formal_result(
            _compatible(legal_evidence_ids=["legal-evidence-" + "2" * 32]),
            context=_context(),
            generation_id="generation-test",
            framework_task_id="task-test",
            framework_run_id="run-test",
        )


def test_formal_payload_does_not_leak_internal_fields() -> None:
    _, payload, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    text = payload.model_dump_json()
    for key in (
        "candidate_id",
        "canonical_root_id",
        "owner_type",
        "routing_rule_id",
        "severity_factor_trace",
    ):
        assert key not in text


def test_dry_run_sink_accepts_then_deduplicates_without_side_effects() -> None:
    _, payload, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    sink = DryRunResultSink()
    first = sink.submit(payload.model_dump(mode="json"))
    second = sink.submit(payload.model_dump(mode="json"))
    assert first.status == "ACCEPTED"
    assert first.would_insert is True
    assert first.write_effect == "NONE"
    assert second.status == "DUPLICATE"
    assert second.would_insert is False
    assert second.would_callback is False


def test_same_review_different_hash_is_a_conflict() -> None:
    _, payload, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    sink = DryRunResultSink()
    sink.submit(payload.model_dump(mode="json"))
    other = _compatible(title="Changed title")
    _, changed, _ = build_formal_result(
        other,
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    with pytest.raises(DirectE2EError, match="different hash"):
        sink.submit(changed.model_dump(mode="json"))


def test_different_generation_requires_independent_review_identity() -> None:
    first_context = _context()
    second_context = _context(review_id="review-generation-2")
    _, first, _ = build_formal_result(
        _compatible(context=first_context),
        context=first_context,
        generation_id="generation-1",
        framework_task_id="task-1",
        framework_run_id="run-1",
    )
    _, second, _ = build_formal_result(
        _compatible(context=second_context),
        context=second_context,
        generation_id="generation-2",
        framework_task_id="task-2",
        framework_run_id="run-2",
    )
    sink = DryRunResultSink()
    assert sink.submit(first.model_dump(mode="json")).status == "ACCEPTED"
    assert sink.submit(second.model_dump(mode="json")).status == "ACCEPTED"


def test_transaction_failure_rolls_back_without_callback() -> None:
    _, payload, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    receipt = DryRunResultSink().submit(
        payload.model_dump(mode="json"),
        fail_transaction=True,
    )
    assert receipt.status == "FAILED"
    assert receipt.transaction_status == "ROLLED_BACK"
    assert receipt.would_insert is False
    assert receipt.would_callback is False


def test_callback_uses_formal_envelope_and_is_idempotent() -> None:
    formal, _, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    callback = build_final_callback(
        formal,
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    sink = DryRunResultSink()
    first = sink.callback(callback)
    second = sink.callback(callback)
    assert callback.stage_id == "finalize_review"
    assert callback.event_sequence == 100
    assert first.status == "ACCEPTED"
    assert second.status == "DUPLICATE"
    assert second.callback_effect == "NONE"


def test_callback_id_reuse_with_changed_payload_is_rejected() -> None:
    formal, _, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    callback = build_final_callback(
        formal,
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    sink = DryRunResultSink()
    sink.callback(callback)
    changed = callback.model_copy(update={"event_sequence": 101})
    with pytest.raises(DirectE2EError, match="callback_id was reused"):
        sink.callback(changed)


def test_callback_failure_has_no_real_effect() -> None:
    formal, _, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    receipt = DryRunResultSink().callback(
        build_final_callback(
            formal,
            framework_task_id="task-test",
            framework_run_id="run-test",
        ),
        fail_callback=True,
    )
    assert receipt.status == "FAILED"
    assert receipt.accepted is False
    assert receipt.callback_effect == "NONE"


@pytest.mark.parametrize("merge_status", ["COMPLETED", "SKIPPED"])
def test_state_trace_reuses_formal_stage_names_and_supports_skipped_merge(
    merge_status: str,
) -> None:
    trace = build_success_state_trace(merge_status=merge_status)
    assert [item.sequence for item in trace] == list(range(1, len(trace) + 1))
    assert trace[0].formal_stage == "RISK_REVIEW"
    assert trace[-1].formal_stage == "FINALIZING"
    assert trace[-1].review_status == "SUCCEEDED"
    assert any(merge_status in item.event for item in trace)


def test_core_result_signature_is_stable_but_changes_for_core_severity() -> None:
    first = _compatible()
    second = _compatible()
    assert core_result_signature(first) == core_result_signature(second)
    changed = _compatible(risk_level="MEDIUM")
    assert core_result_signature(first) != core_result_signature(changed)


def test_core_result_signature_ignores_explanatory_evidence_wording() -> None:
    first = _compatible()
    changed_evidence = first.final_evidence[0].model_copy(
        update={"checked_scope": "Equivalent explanatory wording"}
    )
    changed = first.model_copy(update={"final_evidence": [changed_evidence]})
    assert core_result_signature(first) == core_result_signature(changed)


def test_runner_finalizes_with_zero_legacy_and_zero_formal_side_effects() -> None:
    compatible = _compatible()
    result, payload = DirectRiskReviewEndToEndRunner().finalize(
        _request(),
        run_id="direct-e2e-run-1",
        compatible=compatible,
        compatibility_context=_context(),
        stage_metrics=[_metric("extended_bundle")],
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    assert result.status == "SUCCEEDED"
    assert result.legacy_model_calls == 0
    assert result.legacy_tool_calls == 0
    assert result.write_effect == "NONE"
    assert result.callback_effect == "NONE"
    assert result.pipeline_cutover_effect == "NONE"
    assert result.formal_result_hash == payload.result_hash


def test_runner_rejects_mismatched_review_identity() -> None:
    with pytest.raises(DirectE2EError, match="review_id changed"):
        DirectRiskReviewEndToEndRunner().finalize(
            _request(review_id="other-review"),
            run_id="direct-e2e-run-1",
            compatible=_compatible(),
            compatibility_context=_context(),
            stage_metrics=[_metric("extended_bundle")],
            framework_task_id="task-test",
            framework_run_id="run-test",
        )


def test_result_hash_and_payload_are_stable_for_one_hundred_replays() -> None:
    compatible = _compatible()
    formal_hashes = set()
    result_hashes = set()
    signatures = set()
    for _ in range(100):
        formal, payload, formal_hash = build_formal_result(
            compatible,
            context=_context(),
            generation_id="generation-test",
            framework_task_id="task-test",
            framework_run_id="run-test",
        )
        formal_hashes.add(formal_hash)
        result_hashes.add(payload.result_hash)
        signatures.add(core_result_signature(compatible))
        assert formal.review_id == payload.review_id
    assert len(formal_hashes) == len(result_hashes) == len(signatures) == 1


def test_invalid_result_hash_never_reaches_dry_run_storage() -> None:
    _, payload, _ = build_formal_result(
        _compatible(),
        context=_context(),
        generation_id="generation-test",
        framework_task_id="task-test",
        framework_run_id="run-test",
    )
    raw = payload.model_dump(mode="json")
    raw["result_hash"] = "sha256:" + "0" * 64
    with pytest.raises(DirectE2EError, match="result_hash"):
        DryRunResultSink().submit(raw)


def _metric(stage: str) -> DirectE2EStageMetric:
    return DirectE2EStageMetric(
        stage=stage,
        wall_ms=1,
        model_calls=0,
        repair_calls=0,
        tool_calls=0,
        prompt_tokens=0,
        cached_tokens=0,
        completion_tokens=0,
    )


def _request(**overrides) -> DirectRiskReviewEndToEndRequest:
    raw = {
        "review_id": "review-test",
        "generation_id": "generation-test",
        "contract_hash": "sha256:" + "1" * 64,
        "fixture_id": "fixture-test",
        "execution_mode": "DRY_RUN",
        "contract_ir_stage_result": {"result_type": "CONTRACT_IR_STAGE_V1"},
        "risk_review_context": {"perspective": "PARTY_A"},
    }
    raw.update(overrides)
    return DirectRiskReviewEndToEndRequest.model_validate(raw)


def _context(review_id: str = "review-test") -> LegacyCompatibilityContext:
    return LegacyCompatibilityContext(
        review_id=review_id,
        business_task_id="business-test",
        contract_version_id="version-test",
        generation_id="generation-test",
        contract_hash="sha256:" + "1" * 64,
        contract_profile=ContractProfile(
            contract_type="AUTO",
            party_a={"name": "Party A"},
            party_b={"name": "Party B"},
            perspective="PARTY_A",
            our_party="Party A",
            counterparty="Party B",
            review_attitude="NEUTRAL",
        ),
    )


def _compatible(
    *,
    title: str = "Advance payment risk",
    risk_level: str = "HIGH",
    context: LegacyCompatibilityContext | None = None,
    legal_evidence_ids: list[str] | None = None,
):
    context = context or _context()
    finding_id = "finding-" + "1" * 32
    evidence_id = "evidence-" + "1" * 32
    text = BLOCK_TEXT[:14]
    finding = {
        "finding_local_id": finding_id,
        "source_unit_id": "commercial_financial",
        "domain": "commercial_financial",
        "check_code": "CF-005",
        "category": "PAYMENT",
        "risk_type": "ADVANCE_PAYMENT_SECURITY_RISK",
        "risk_level": risk_level,
        "title": title,
        "issue": "The contract requires advance payment without security.",
        "impact_to_our_party": "Our party bears unsecured prepayment exposure.",
        "suggestion": "Add a performance guarantee or milestone payments.",
        "perspective": "PARTY_A",
        "our_party": context.contract_profile.our_party,
        "counterparty": context.contract_profile.counterparty,
        "evidence_candidates": [
            {
                "evidence_local_id": evidence_id,
                "finding_local_id": finding_id,
                "evidence_type": "TEXT_QUOTE",
                "source_ir_item_id": "I001",
                "anchor_id": "A001",
                "block_id": "block-1",
                "page_number": None,
                "char_start": 0,
                "char_end": 14,
                "quoted_text": text,
                "quoted_text_hash": "sha256:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "checked_scope": None,
                "verification_note": None,
            }
        ],
        "legal_evidence_ids": legal_evidence_ids or [],
    }
    bundle = {
        "bundle_id": "extended-bundle-test",
        "findings": [finding],
        "base_bundle": {
            "units": [
                {
                    "unit_id": "commercial_financial",
                    "canonical_risk_roots": [
                        {
                            "root_id": "root-1",
                            "finding_local_id": finding_id,
                        }
                    ],
                }
            ]
        },
        "horizontal_units": [],
    }
    projection = LegacyRiskArtifactAdapter().adapt(bundle)
    return finalize_legacy_compatible_result(
        projection,
        context=context,
        consolidation=FindingConsolidationArtifact(
            result_type="FINDING_CONSOLIDATION_V1",
            status="COMPLETED",
            candidate_count=0,
            model_call_count=0,
            decisions=[],
            skip_reason=None,
        ),
        blocks=[BLOCK],
    )
