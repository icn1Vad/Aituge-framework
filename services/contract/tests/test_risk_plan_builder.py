from __future__ import annotations

import pytest

from contract.api.models import FindingCategory
from contract.errors import ContractError
from contract.risk.models import (
    Criticality,
    ExecutionMode,
    PlaybookManifest,
    RiskHorizontalCandidate,
    RiskSourceBlock,
    SpecialistReviewerSpec,
)
from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.risk.playbooks import (
    BASE_UNIT_IDS,
    HORIZONTAL_UNIT_IDS,
    PlaybookRegistry,
    PlaybookRouter,
    build_default_registry,
)

from risk_test_data import risk_plan_input


def _extension(base: PlaybookManifest, check_code: str = "CF-001") -> PlaybookManifest:
    return base.model_copy(
        update={
            "playbook_id": "payment_extension",
            "name": "付款检查扩展包",
            "description": "在既有商业财务审查单元内扩展检查。",
            "execution_mode": ExecutionMode.EXTEND_DOMAIN,
            "target_domains": ["commercial_financial"],
            "check_codes": [check_code],
        }
    )


def _specialist(base: PlaybookManifest, index: int) -> PlaybookManifest:
    return base.model_copy(
        update={
            "playbook_id": f"specialist_{index}",
            "name": f"专项审查{index}",
            "description": "测试专项审查上限。",
            "execution_mode": ExecutionMode.SPECIALIST_REVIEWER,
            "target_domains": ["commercial_financial"],
            "check_codes": ["CF-001"],
            "specialist_reviewer_spec": SpecialistReviewerSpec(
                specialist_id=f"specialist-{index}",
                trigger_check_codes=["CF-001"],
            ),
        }
    )


def test_frozen_registry_contains_exactly_45_unique_checks_and_mappings() -> None:
    registry = build_default_registry()

    codes = [item.check_code for item in registry.checks]
    mappings = [(item.domain, item.check_code, item.category) for item in registry.mappings]
    other = [item for item in registry.checks if item.allowed_categories == [FindingCategory.OTHER]]

    assert len(codes) == 45
    assert len(codes) == len(set(codes))
    assert len(mappings) == len(set(mappings)) == 45
    assert len(other) == 1
    assert other[0].check_code == "FVA-005"
    assert other[0].domain == "formation_validity_authority"
    assert other[0].allowed_risk_types == ["MANDATORY_RULE_OR_VALIDITY_RISK"]


def test_plan_is_fully_deterministic_and_projects_only_relevant_ir() -> None:
    value = risk_plan_input()
    builder = RiskReviewPlanBuilder()

    first = builder.build(value)
    second = builder.build(value)

    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.review_units) == 7
    assert first.required_unit_ids == list(BASE_UNIT_IDS)
    assert [item.unit_id for item in first.review_units] == list(BASE_UNIT_IDS + HORIZONTAL_UNIT_IDS)
    assert sum(len(item.check_specs) for item in first.review_units) == 45
    assert len(first.deterministic_checks) == 11
    assert all(item.status == "REVIEWED" for item in first.deterministic_checks)

    horizontal = [item for item in first.review_units if item.unit_id in HORIZONTAL_UNIT_IDS]
    assert all(item.requires_model is False for item in horizontal)
    assert all(item.batch_ids == [] for item in horizontal)

    total_ir_items = sum(
        len(value.stage_result.semantic_ir.model_dump(mode="json")[field])
        for field in type(value.stage_result.semantic_ir).model_fields
    )
    assert total_ir_items == 14
    assert first.contexts
    assert all(context.coverage_summary.projected_ir_item_count < total_ir_items for context in first.contexts)
    assert all(context.context_hash.startswith("sha256:") for context in first.contexts)
    assert all(context.plan_id == first.plan_id for context in first.contexts)
    assert all(context.clause_catalog or context.absence_evidence_sources for context in first.contexts)
    unit_by_id = {unit.unit_id: unit for unit in first.review_units}
    assert all(
        context.estimated_input_tokens <= unit_by_id[context.unit_id].hard_input_token_limit
        for context in first.contexts
    )

    source = value.source_blocks[0].text
    for context in first.contexts:
        for excerpt in context.source_excerpts:
            assert excerpt.quoted_text == source[excerpt.char_start : excerpt.char_end]


def test_extend_domain_reuses_existing_units_and_does_not_increase_unit_count() -> None:
    default = build_default_registry()
    base = default.manifest("base_neutral")
    extension_check = default.check("CF-001").model_copy(
        update={
            "check_code": "SWI-001",
            "title": "软件成果知识产权专项检查",
            "description": "检查软件成果及其知识产权归属。",
            "domain": "ip_confidentiality_data",
            "required_ir_types": ["intellectual_property_terms", "rights", "obligations"],
            "review_question": "检查软件成果和新增知识产权是否清晰归属并保护我方。",
            "allowed_categories": [FindingCategory.INTELLECTUAL_PROPERTY],
            "allowed_risk_types": ["SOFTWARE_IP_OWNERSHIP_RISK"],
            "legacy_artifact_type": "liability_termination_review_result",
            "priority": 900,
        }
    )
    registry = PlaybookRegistry(
        manifests=[
            base,
            _extension(base, "SWI-001").model_copy(
                update={"target_domains": ["ip_confidentiality_data"]}
            ),
        ],
        checks=[*default.checks, extension_check],
    )
    builder = RiskReviewPlanBuilder(registry=registry, router=PlaybookRouter(registry))
    value = risk_plan_input().model_copy(
        update={"selected_playbook_ids": ["base_neutral", "payment_extension"]}
    )

    plan = builder.build(value)

    assert len(plan.review_units) == 7
    assert [item.unit_id for item in plan.review_units] == list(BASE_UNIT_IDS + HORIZONTAL_UNIT_IDS)
    assert plan.selected_playbook_ids == ["base_neutral", "payment_extension"]
    assert sum(len(item.check_specs) for item in plan.review_units) == 46


def test_router_rejects_unknown_missing_base_duplicate_and_more_than_two_specialists() -> None:
    default = build_default_registry()
    base = default.manifest("base_neutral")
    specialists = [_specialist(base, index) for index in range(1, 4)]
    registry = PlaybookRegistry(
        manifests=[base, *specialists],
        checks=default.checks,
    )
    router = PlaybookRouter(registry)
    common = {
        "contract_type": "AUTO",
        "perspective": "PARTY_A",
        "review_attitude": "NEUTRAL",
    }

    with pytest.raises(ContractError, match="base_neutral"):
        router.route(["specialist_1"], **common)
    with pytest.raises(ContractError, match="Unknown risk review playbook"):
        router.route(["base_neutral", "not_exists"], **common)
    with pytest.raises(ContractError, match="must be unique"):
        router.route(["base_neutral", "base_neutral"], **common)
    with pytest.raises(ContractError, match="At most two"):
        router.route(
            ["base_neutral", "specialist_1", "specialist_2", "specialist_3"],
            **common,
        )


def test_registry_rejects_any_other_mapping_except_frozen_fva_005() -> None:
    default = build_default_registry()
    base = default.manifest("base_neutral")

    altered_fva = [
        item.model_copy(update={"allowed_risk_types": ["WRONG"]})
        if item.check_code == "FVA-005"
        else item
        for item in default.checks
    ]
    with pytest.raises(ValueError, match="Only FVA-005"):
        PlaybookRegistry(manifests=[base], checks=altered_fva)

    added_other = [
        item.model_copy(update={"allowed_categories": [FindingCategory.OTHER]})
        if item.check_code == "CF-001"
        else item
        for item in default.checks
    ]
    with pytest.raises(ValueError, match="Only FVA-005"):
        PlaybookRegistry(manifests=[base], checks=added_other)


def test_horizontal_candidate_must_reference_known_check_ir_and_anchor() -> None:
    value = risk_plan_input()
    candidate = RiskHorizontalCandidate(
        candidate_id="candidate-" + "1" * 32,
        candidate_type="OBLIGATION_CONFLICT",
        check_code="CCC-002",
        item_ids=["item-rights"],
        anchor_ids=["anchor-1"],
        reason_code="SYNTHETIC_CONFLICT",
    )

    plan = RiskReviewPlanBuilder().build(
        value.model_copy(update={"horizontal_candidates": [candidate]})
    )
    consistency = next(
        item for item in plan.review_units if item.unit_id == "cross_clause_consistency"
    )
    assert consistency.requires_model is True
    assert len(consistency.batch_ids) == 1

    invalid = candidate.model_copy(update={"item_ids": ["missing-item"]})
    with pytest.raises(ContractError, match="Horizontal candidates"):
        RiskReviewPlanBuilder().build(
            value.model_copy(update={"horizontal_candidates": [invalid]})
        )


def test_strict_models_reject_invalid_enum_values() -> None:
    with pytest.raises(ValueError):
        Criticality("MUST")
    with pytest.raises(ValueError):
        ExecutionMode("AGENT")


@pytest.mark.parametrize("unit_id,field,prefix,limit", [
    ("commercial_financial", "payment_terms", "CF", 6000),
    ("performance_obligations", "obligations", "PO", 4000),
])
def test_oversized_commercial_context_is_sharded_without_losing_items(unit_id, field, prefix, limit) -> None:
    value = risk_plan_input()
    semantic_ir = value.stage_result.semantic_ir.model_dump(mode="json")
    payment_items = []
    extra_blocks = []
    for index in range(1, 13):
        text = f"第{index}组付款条件：" + "甲方验收后按节点支付对应款项。" * 60
        block_id = f"block-payment-{index:02d}"
        anchor = {
            "anchor_id": f"anchor-payment-{index:02d}",
            "block_id": block_id,
            "page_number": index,
            "char_start": 0,
            "char_end": len(text),
        }
        payment_items.append(
            {
                "item_id": f"item-payment-{index:02d}",
                "subject": "甲方",
                "predicate": "应支付",
                "object": f"第{index}组款项",
                "source_anchors": [anchor],
            }
        )
        extra_blocks.append(
            RiskSourceBlock(
                block_id=block_id,
                block_no=index + 1,
                page_number=index,
                text=text,
            )
        )
    semantic_ir[field] = payment_items
    stage_result = type(value.stage_result).model_validate(
        {
            **value.stage_result.model_dump(mode="json"),
            "semantic_ir": semantic_ir,
        }
    )
    plan = RiskReviewPlanBuilder().build(
        value.model_copy(
            update={
                "stage_result": stage_result,
                "source_blocks": [*value.source_blocks, *extra_blocks],
            }
        )
    )

    commercial = next(
        unit
        for unit in plan.review_units
        if unit.unit_id == unit_id
    )
    contexts = [
        context
        for context in plan.contexts
        if context.unit_id == unit_id
    ]
    assert len(commercial.batch_ids) == len(contexts) > 1
    if unit_id == "commercial_financial":
        assert any(len(context.check_specs) < 8 for context in contexts)
        assert all(len(context.check_task_scopes) == len(context.check_specs) for context in contexts)
    assert {spec.check_code for context in contexts for spec in context.check_specs} == {
        spec.check_code for spec in commercial.check_specs
    }
    assert all(context.estimated_input_tokens <= limit or (
        len(context.check_specs) == 1 and context.estimated_input_tokens <= 24000
        and all(scope.complete for scope in context.check_task_scopes)
    ) for context in contexts)
    projected_ids = [
        item.item_id
        for context in contexts
        for item in [*context.definitions, *context.projected_ir_items]
    ]
    for expected in (f"item-payment-{index:02d}" for index in range(1, 13)):
        assert expected in projected_ids
    for context in contexts:
        scopes = {scope.check_code: scope for scope in context.check_task_scopes}
        assert all(scopes[source.check_code].complete for source in context.absence_evidence_sources)
