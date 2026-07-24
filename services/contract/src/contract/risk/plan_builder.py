from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Iterable

from contract.application.idempotency import canonical_json
from contract.errors import ContractError
from contract.risk.icd_source_policy import (
    ICD_ABSENCE_POLICIES,
    icd_mechanism_is_complete,
    icd_item_matches_check,
)
from contract.risk.lre_source_policy import (
    LRE_ABSENCE_POLICIES,
    lre_item_matches_check,
    lre_mechanism_is_complete,
)
from contract.risk.models import (
    CheckSpec,
    DeterministicCheckResult,
    IrField,
    ReviewBatchSpec,
    ReviewUnitSpec,
    ReviewUnitType,
    RiskAbsenceEvidenceSource,
    RiskClauseCatalogItem,
    RiskCheckEvidencePolicy,
    RiskCoverageSummary,
    RiskDomain,
    RiskEvidenceSource,
    RiskHorizontalCandidate,
    RiskProjectedIrItem,
    RiskReviewContext,
    RiskReviewPlan,
    RiskReviewPlanInput,
    RiskSourceBlock,
    RiskSourceExcerpt,
)
from contract.risk.playbooks import (
    BASE_UNIT_IDS,
    HORIZONTAL_UNIT_IDS,
    UNIT_ORDER,
    PlaybookRegistry,
    PlaybookRouter,
    build_default_registry,
)
from contract.risk.po_source_policy import po_item_matches_check


ZERO_HASH = "sha256:" + "0" * 64
ZERO_PLAN_ID = "risk-plan-" + "0" * 32


class RiskReviewPlanBuilder:
    def __init__(
        self,
        registry: PlaybookRegistry | None = None,
        router: PlaybookRouter | None = None,
    ) -> None:
        self.registry = registry or build_default_registry()
        self.router = router or PlaybookRouter(self.registry)

    def build(self, value: RiskReviewPlanInput) -> RiskReviewPlan:
        route = self.router.route(
            value.selected_playbook_ids,
            contract_type=value.contract_type,
            perspective=value.perspective,
            review_attitude=value.review_attitude,
        )
        checks = self.registry.checks_for(route.selected)
        self._validate_check_set(checks)
        all_items = self._projected_items(value)
        blocks = self._source_blocks(value.source_blocks)
        excerpts = self._source_excerpts(all_items, blocks)
        candidates = self._horizontal_candidates(
            value.horizontal_candidates,
            checks,
            all_items,
            excerpts,
        )

        checks_by_domain: dict[RiskDomain, list[CheckSpec]] = defaultdict(list)
        for check in checks:
            checks_by_domain[check.domain].append(check)

        units: list[ReviewUnitSpec] = []
        contexts: list[RiskReviewContext] = []
        deterministic_checks: list[DeterministicCheckResult] = []
        for unit_id in UNIT_ORDER:
            unit_checks = tuple(checks_by_domain[unit_id])
            if not unit_checks:
                raise ContractError(
                    "RISK_PLAN_INVALID",
                    f"Required review unit has no checks: {unit_id}",
                    status_code=422,
                )
            unit_candidates = tuple(item for item in candidates if item.check_code in {
                check.check_code for check in unit_checks
            })
            executable_checks = unit_checks
            if unit_id in HORIZONTAL_UNIT_IDS:
                candidate_codes = {item.check_code for item in unit_candidates}
                executable_checks = tuple(
                    check for check in unit_checks if check.check_code in candidate_codes
                )
                deterministic_checks.extend(
                    DeterministicCheckResult(
                        check_code=check.check_code,
                        status="REVIEWED",
                        reason_code="NO_DETERMINISTIC_CANDIDATES",
                    )
                    for check in unit_checks
                    if check.check_code not in candidate_codes
                )

            batch_contexts, batches = self._batches(
                value=value,
                unit_id=unit_id,
                checks=executable_checks,
                all_items=all_items,
                excerpts=excerpts,
                candidates=unit_candidates,
            )
            contexts.extend(batch_contexts)
            units.append(
                self._unit_spec(
                    unit_id=unit_id,
                    checks=unit_checks,
                    batches=batches,
                )
            )

        plan_hash = self._plan_hash(
            value=value,
            selected_playbook_ids=[item.playbook_id for item in route.selected],
            units=units,
            contexts=contexts,
            deterministic_checks=deterministic_checks,
            specialist_count=route.specialist_count,
        )
        plan_id = "risk-plan-" + plan_hash.removeprefix("sha256:")[:32]
        final_contexts = [self._finalize_context(context, plan_id) for context in contexts]
        return RiskReviewPlan(
            plan_id=plan_id,
            plan_hash=plan_hash,
            review_id=value.review_id,
            document_id=value.document_id,
            generation_id=value.generation_id,
            attempt_no=value.attempt_no,
            perspective=value.perspective,
            contract_type=value.contract_type,
            review_attitude=value.review_attitude,
            selected_playbook_ids=[item.playbook_id for item in route.selected],
            review_units=units,
            contexts=final_contexts,
            deterministic_checks=sorted(
                deterministic_checks,
                key=lambda item: item.check_code,
            ),
            specialist_reviewer_count=route.specialist_count,
            max_concurrency=7,
            required_unit_ids=list(BASE_UNIT_IDS),
        )

    @staticmethod
    def _validate_check_set(checks: tuple[CheckSpec, ...]) -> None:
        codes = [item.check_code for item in checks]
        if len(codes) != len(set(codes)):
            raise ContractError(
                "RISK_PLAN_INVALID",
                "Risk review checks are duplicated",
                status_code=422,
            )

    @staticmethod
    def _source_blocks(values: list[RiskSourceBlock]) -> dict[str, RiskSourceBlock]:
        result: dict[str, RiskSourceBlock] = {}
        block_numbers: set[int] = set()
        for value in values:
            if value.block_id in result or value.block_no in block_numbers:
                raise ContractError(
                    "RISK_SOURCE_INVALID",
                    "Risk review source Blocks must be unique",
                    status_code=422,
                )
            result[value.block_id] = value
            block_numbers.add(value.block_no)
        return result

    @staticmethod
    def _projected_items(value: RiskReviewPlanInput) -> tuple[RiskProjectedIrItem, ...]:
        result: list[RiskProjectedIrItem] = []
        semantic = value.stage_result.semantic_ir
        for definition in semantic.definitions:
            digest = hashlib.sha256(
                canonical_json(definition.model_dump(mode="json")).encode("utf-8")
            ).hexdigest()[:32]
            result.append(
                RiskProjectedIrItem(
                    ir_type="definitions",
                    item_id=f"definition-{digest}",
                    subject=definition.term,
                    predicate="定义为",
                    object=definition.meaning,
                    source_anchors=definition.source_anchors,
                )
            )
        for ir_type in (
            "rights",
            "obligations",
            "prohibitions",
            "payment_terms",
            "delivery_terms",
            "acceptance_terms",
            "liabilities",
            "termination_terms",
            "confidentiality_terms",
            "intellectual_property_terms",
            "dispute_resolution",
            "dates",
            "amounts",
        ):
            for item in getattr(semantic, ir_type):
                result.append(
                    RiskProjectedIrItem(
                        ir_type=ir_type,
                        item_id=item.item_id,
                        subject=item.subject,
                        predicate=item.predicate,
                        object=item.object,
                        source_anchors=item.source_anchors,
                    )
                )
        item_ids = [item.item_id for item in result]
        if len(item_ids) != len(set(item_ids)):
            raise ContractError(
                "RISK_SOURCE_INVALID",
                "Contract IR item IDs must be unique",
                status_code=422,
            )
        return tuple(sorted(result, key=lambda item: (item.ir_type, item.item_id)))

    @staticmethod
    def _source_excerpts(
        items: tuple[RiskProjectedIrItem, ...],
        blocks: dict[str, RiskSourceBlock],
    ) -> dict[str, RiskSourceExcerpt]:
        result: dict[str, RiskSourceExcerpt] = {}
        for item in items:
            for anchor in item.source_anchors:
                block = blocks.get(anchor.block_id)
                if block is None or anchor.char_end > len(block.text):
                    raise ContractError(
                        "RISK_SOURCE_INVALID",
                        "Contract IR Anchor does not resolve to the fixed source Block",
                        status_code=422,
                        details={"anchor_id": anchor.anchor_id, "block_id": anchor.block_id},
                    )
                quoted_text = block.text[anchor.char_start : anchor.char_end]
                if not quoted_text:
                    raise ContractError(
                        "RISK_SOURCE_INVALID",
                        "Contract IR Anchor resolves to empty text",
                        status_code=422,
                    )
                excerpt = RiskSourceExcerpt(
                    anchor_id=anchor.anchor_id,
                    block_id=anchor.block_id,
                    block_no=block.block_no,
                    page_number=anchor.page_number,
                    char_start=anchor.char_start,
                    char_end=anchor.char_end,
                    quoted_text=quoted_text,
                    quoted_text_hash="sha256:"
                    + hashlib.sha256(quoted_text.encode("utf-8")).hexdigest(),
                    heading_path=block.heading_path,
                )
                previous = result.get(anchor.anchor_id)
                if previous is not None and previous != excerpt:
                    raise ContractError(
                        "RISK_SOURCE_INVALID",
                        "The same Anchor ID resolves to different source text",
                        status_code=422,
                    )
                result[anchor.anchor_id] = excerpt
        return result

    def _horizontal_candidates(
        self,
        values: list[RiskHorizontalCandidate],
        checks: tuple[CheckSpec, ...],
        items: tuple[RiskProjectedIrItem, ...],
        excerpts: dict[str, RiskSourceExcerpt],
    ) -> tuple[RiskHorizontalCandidate, ...]:
        allowed = {
            item.check_code
            for item in checks
            if item.domain in HORIZONTAL_UNIT_IDS
        }
        known_item_ids = {item.item_id for item in items}
        known_anchor_ids = set(excerpts)
        ids: set[str] = set()
        for value in values:
            invalid_references = (
                set(value.item_ids) - known_item_ids
                or set(value.anchor_ids) - known_anchor_ids
            )
            if (
                value.candidate_id in ids
                or value.check_code not in allowed
                or len(value.item_ids) != len(set(value.item_ids))
                or len(value.anchor_ids) != len(set(value.anchor_ids))
                or invalid_references
            ):
                raise ContractError(
                    "RISK_CANDIDATE_INVALID",
                    "Horizontal candidates must be unique and reference assigned checks, IR and Anchors",
                    status_code=422,
                )
            ids.add(value.candidate_id)
        return tuple(sorted(values, key=lambda item: item.candidate_id))

    def _batches(
        self,
        *,
        value: RiskReviewPlanInput,
        unit_id: RiskDomain,
        checks: tuple[CheckSpec, ...],
        all_items: tuple[RiskProjectedIrItem, ...],
        excerpts: dict[str, RiskSourceExcerpt],
        candidates: tuple[RiskHorizontalCandidate, ...],
    ) -> tuple[list[RiskReviewContext], list[ReviewBatchSpec]]:
        if not checks:
            return [], []
        _soft_limit, hard_limit = self._input_limits(unit_id)
        groups = self._partition_checks(
            value=value,
            unit_id=unit_id,
            checks=checks,
            all_items=all_items,
            excerpts=excerpts,
            candidates=candidates,
            hard_limit=hard_limit,
        )

        contexts: list[RiskReviewContext] = []
        batches: list[ReviewBatchSpec] = []
        for group in groups:
            ir_types = self._ir_types(group)
            projected = self._select_items(all_items, ir_types, candidates)
            selected_excerpts = self._select_excerpts(projected, excerpts, candidates)
            estimate = self._estimate_context(value, group, projected, selected_excerpts, candidates)
            batch_id = self._stable_id(
                "risk-batch",
                {
                    "review_id": value.review_id,
                    "generation_id": value.generation_id,
                    "attempt_no": value.attempt_no,
                    "unit_id": unit_id,
                    "check_codes": [item.check_code for item in group],
                },
            )
            batch = ReviewBatchSpec(
                batch_id=batch_id,
                check_codes=[item.check_code for item in group],
                required_ir_types=list(ir_types),
                projected_item_ids=[item.item_id for item in projected],
                source_anchor_ids=[item.anchor_id for item in selected_excerpts],
                estimated_input_tokens=estimate,
            )
            batches.append(batch)
            contexts.append(
                self._context(
                    value=value,
                    unit_id=unit_id,
                    batch=batch,
                    checks=group,
                    projected=projected,
                    excerpts=selected_excerpts,
                    candidates=candidates,
                    total_ir_item_count=len(all_items),
                )
            )
        return contexts, batches

    def _partition_checks(
        self,
        *,
        value: RiskReviewPlanInput,
        unit_id: RiskDomain,
        checks: tuple[CheckSpec, ...],
        all_items: tuple[RiskProjectedIrItem, ...],
        excerpts: dict[str, RiskSourceExcerpt],
        candidates: tuple[RiskHorizontalCandidate, ...],
        hard_limit: int,
    ) -> list[tuple[CheckSpec, ...]]:
        """Find the stable minimum Batch partition for a small domain check set."""
        count = len(checks)
        if count > 12:
            raise ContractError(
                "RISK_PLAN_INVALID",
                f"Too many checks in one deterministic review unit: {unit_id}",
                status_code=422,
            )
        estimates: dict[int, int] = {}
        valid_masks: set[int] = set()
        for mask in range(1, 1 << count):
            group = tuple(checks[index] for index in range(count) if mask & (1 << index))
            estimate = self._estimate_for_checks(
                value,
                group,
                all_items,
                excerpts,
                candidates,
            )
            estimates[mask] = estimate
            if estimate <= hard_limit:
                valid_masks.add(mask)

        for index, check in enumerate(checks):
            mask = 1 << index
            if mask not in valid_masks:
                raise ContractError(
                    "RISK_CONTEXT_BUDGET_EXCEEDED",
                    f"A single check exceeds the {unit_id} Business Context budget",
                    status_code=422,
                    details={
                        "check_code": check.check_code,
                        "estimated_tokens": estimates[mask],
                    },
                )

        memo: dict[int, tuple[int, ...]] = {0: ()}

        def solve(remaining: int) -> tuple[int, ...]:
            cached = memo.get(remaining)
            if cached is not None:
                return cached
            first_bit = remaining & -remaining
            subset = remaining
            candidates_for_state: list[tuple[int, ...]] = []
            while subset:
                if subset & first_bit and subset in valid_masks:
                    rest = solve(remaining ^ subset)
                    candidates_for_state.append((subset, *rest))
                subset = (subset - 1) & remaining

            if not candidates_for_state:
                raise ContractError(
                    "RISK_CONTEXT_BUDGET_EXCEEDED",
                    f"Checks in {unit_id} cannot be partitioned within the Business Context budget",
                    status_code=422,
                )

            def partition_key(masks: tuple[int, ...]) -> tuple[object, ...]:
                code_groups = tuple(
                    tuple(
                        checks[index].check_code
                        for index in range(count)
                        if mask & (1 << index)
                    )
                    for mask in masks
                )
                return (
                    len(masks),
                    max(estimates[mask] for mask in masks),
                    code_groups,
                )

            result = min(candidates_for_state, key=partition_key)
            memo[remaining] = result
            return result

        masks = solve((1 << count) - 1)
        return [
            tuple(checks[index] for index in range(count) if mask & (1 << index))
            for mask in masks
        ]

    def _context(
        self,
        *,
        value: RiskReviewPlanInput,
        unit_id: RiskDomain,
        batch: ReviewBatchSpec,
        checks: tuple[CheckSpec, ...],
        projected: tuple[RiskProjectedIrItem, ...],
        excerpts: tuple[RiskSourceExcerpt, ...],
        candidates: tuple[RiskHorizontalCandidate, ...],
        total_ir_item_count: int,
    ) -> RiskReviewContext:
        present = sorted({item.ir_type for item in projected})
        required = self._ir_types(checks)
        missing = sorted(set(required) - set(present))
        definitions = [item for item in projected if item.ir_type == "definitions"]
        other_items = [item for item in projected if item.ir_type != "definitions"]
        evidence_sources = self._evidence_sources(
            value.generation_id,
            unit_id,
            checks,
            projected,
            excerpts,
        )
        absence_sources = self._absence_evidence_sources(
            value.generation_id,
            unit_id,
            checks,
            present,
            evidence_sources,
        )
        evidence_policies = self._check_evidence_policies(
            checks,
            evidence_sources,
            absence_sources,
        )
        raw = RiskReviewContext(
            review_id=value.review_id,
            document_id=value.document_id,
            generation_id=value.generation_id,
            attempt_no=value.attempt_no,
            plan_id=ZERO_PLAN_ID,
            unit_id=unit_id,
            batch_id=batch.batch_id,
            perspective=value.perspective,
            our_party=value.our_party,
            counterparty=value.counterparty,
            contract_type=value.contract_type,
            review_attitude=value.review_attitude,
            check_specs=list(checks),
            definitions=definitions,
            projected_ir_items=other_items,
            clause_catalog=self._clause_catalog(excerpts),
            source_excerpts=list(excerpts),
            source_anchor_index=list(excerpts),
            evidence_sources=evidence_sources,
            absence_evidence_sources=absence_sources,
            check_evidence_policies=evidence_policies,
            present_ir_types=present,
            missing_ir_types=missing,
            coverage_summary=RiskCoverageSummary(
                available_ir_types=present,
                missing_ir_types=missing,
                total_ir_item_count=total_ir_item_count,
                projected_ir_item_count=len(projected),
                source_block_count=len({item.block_id for item in excerpts}),
                source_excerpt_count=len(excerpts),
            ),
            horizontal_candidates=list(candidates),
            estimated_input_tokens=batch.estimated_input_tokens,
            context_hash=ZERO_HASH,
        )
        return raw

    @classmethod
    def _evidence_sources(
        cls,
        generation_id: str,
        unit_id: RiskDomain,
        checks: tuple[CheckSpec, ...],
        projected: tuple[RiskProjectedIrItem, ...],
        excerpts: tuple[RiskSourceExcerpt, ...],
    ) -> list[RiskEvidenceSource]:
        excerpts_by_anchor = {item.anchor_id: item for item in excerpts}
        result: list[RiskEvidenceSource] = []
        for item in projected:
            item_excerpts = [
                excerpts_by_anchor[anchor.anchor_id]
                for anchor in item.source_anchors
                if anchor.anchor_id in excerpts_by_anchor
            ]
            if len(item_excerpts) != len(item.source_anchors):
                raise ContractError(
                    "RISK_SOURCE_INVALID",
                    "Evidence Source references an Anchor outside the Batch",
                    status_code=422,
                )
            allowed_check_codes = [
                check.check_code
                for check in checks
                if (
                    po_item_matches_check(item, item_excerpts, check)
                    if unit_id == "performance_obligations"
                    else (
                        icd_item_matches_check(item, item_excerpts, check)
                        if unit_id == "ip_confidentiality_data"
                        else (
                            lre_item_matches_check(item, item_excerpts, check)
                            if unit_id == "liability_remedies_exit"
                            else item.ir_type in check.required_ir_types
                        )
                    )
                )
            ]
            if not allowed_check_codes:
                continue
            for anchor in item.source_anchors:
                excerpt = excerpts_by_anchor.get(anchor.anchor_id)
                if excerpt is None:
                    raise ContractError(
                        "RISK_SOURCE_INVALID",
                        "Evidence Source references an Anchor outside the Batch",
                        status_code=422,
                    )
                source_id = cls._stable_id(
                    "risk-es",
                    {
                        "generation_id": generation_id,
                        "ir_item_id": item.item_id,
                        "anchor_id": excerpt.anchor_id,
                        "char_start": excerpt.char_start,
                        "char_end": excerpt.char_end,
                        "evidence_type": "TEXT_QUOTE",
                    },
                )
                result.append(
                    RiskEvidenceSource(
                        source_id=source_id,
                        generation_id=generation_id,
                        ir_item_id=item.item_id,
                        anchor_id=excerpt.anchor_id,
                        block_id=excerpt.block_id,
                        page_number=excerpt.page_number,
                        char_start=excerpt.char_start,
                        char_end=excerpt.char_end,
                        quoted_text=excerpt.quoted_text,
                        quoted_text_hash=excerpt.quoted_text_hash,
                        evidence_type="TEXT_QUOTE",
                        ir_type=item.ir_type,
                        subject=item.subject,
                        predicate=item.predicate,
                        object=item.object,
                        heading_path=excerpt.heading_path,
                        allowed_check_codes=allowed_check_codes,
                    )
                )
        source_ids = [item.source_id for item in result]
        bindings = [
            (item.ir_item_id, item.anchor_id, item.evidence_type)
            for item in result
        ]
        if len(source_ids) != len(set(source_ids)) or len(bindings) != len(
            set(bindings)
        ):
            raise ContractError(
                "RISK_SOURCE_INVALID",
                "Evidence Source IDs and IR/Anchor bindings must be unique",
                status_code=422,
            )
        return sorted(
            result,
            key=lambda item: (
                item.ir_type,
                item.ir_item_id,
                item.anchor_id,
                item.source_id,
            ),
        )

    @classmethod
    def _absence_evidence_sources(
        cls,
        generation_id: str,
        unit_id: RiskDomain,
        checks: tuple[CheckSpec, ...],
        present_ir_types: list[IrField],
        evidence_sources: list[RiskEvidenceSource],
    ) -> list[RiskAbsenceEvidenceSource]:
        result = []
        for check in checks:
            if (
                unit_id == "ip_confidentiality_data"
                and check.check_code in ICD_ABSENCE_POLICIES
            ):
                if icd_mechanism_is_complete(
                    check.check_code,
                    evidence_sources,
                ):
                    continue
                (
                    checked_target,
                    verification_method,
                    missing_target,
                ) = ICD_ABSENCE_POLICIES[check.check_code]
                checked_scope = (
                    f"当前Batch全部ICD领域IR与Source Excerpt；"
                    f"检查项{check.check_code}；检查范围：{checked_target}"
                )
                verification_method = (
                    verification_method
                    + "；仅证明当前合同技术文本中未定位到该机制，"
                    "不推断外部制度、系统或现实履行事实。"
                )
            elif unit_id == "liability_remedies_exit":
                if check.check_code not in LRE_ABSENCE_POLICIES:
                    continue
                if lre_mechanism_is_complete(
                    check.check_code,
                    evidence_sources,
                ):
                    continue
                (
                    checked_target,
                    verification_method,
                    missing_target,
                ) = LRE_ABSENCE_POLICIES[check.check_code]
                checked_scope = (
                    f"当前Batch全部LRE领域IR与Source Excerpt；"
                    f"检查项{check.check_code}；检查范围：{checked_target}"
                )
                verification_method = (
                    verification_method
                    + "；仅证明当前合同文本中未定位到该机制，"
                    "不推断合同外事实。"
                )
            else:
                checked_scope = (
                    f"当前Batch投影的合同IR与Source Excerpt；检查项{check.check_code}"
                )
                verification_method = (
                    "Python按CheckSpec.required_ir_types及确定性候选扫描当前Batch；"
                    "仅证明本次合同文本投影中未定位到目标条款，不推断外部事实。"
                )
                missing_target = check.review_question
            source_id = cls._stable_id(
                "risk-as",
                {
                    "generation_id": generation_id,
                    "check_code": check.check_code,
                    "checked_scope": checked_scope,
                    "verification_method": verification_method,
                    "present_ir_types": present_ir_types,
                    "missing_target": missing_target,
                },
            )
            result.append(
                RiskAbsenceEvidenceSource(
                    source_id=source_id,
                    generation_id=generation_id,
                    check_code=check.check_code,
                    checked_scope=checked_scope,
                    verification_method=verification_method,
                    present_ir_types=present_ir_types,
                    missing_target=missing_target,
                )
            )
        return result

    @staticmethod
    def _check_evidence_policies(
        checks: tuple[CheckSpec, ...],
        evidence_sources: list[RiskEvidenceSource],
        absence_sources: list[RiskAbsenceEvidenceSource],
    ) -> list[RiskCheckEvidencePolicy]:
        absence_by_check = {
            item.check_code: item.source_id for item in absence_sources
        }
        return [
            RiskCheckEvidencePolicy(
                check_code=check.check_code,
                allowed_evidence_source_ids=[
                    item.source_id
                    for item in evidence_sources
                    if check.check_code in item.allowed_check_codes
                ],
                allowed_absence_source_ids=(
                    [absence_by_check[check.check_code]]
                    if check.check_code in absence_by_check
                    else []
                ),
            )
            for check in checks
        ]

    @staticmethod
    def _clause_catalog(
        excerpts: tuple[RiskSourceExcerpt, ...],
    ) -> list[RiskClauseCatalogItem]:
        grouped: dict[str, list[RiskSourceExcerpt]] = defaultdict(list)
        for excerpt in excerpts:
            grouped[excerpt.block_id].append(excerpt)
        return [
            RiskClauseCatalogItem(
                block_id=values[0].block_id,
                block_no=values[0].block_no,
                page_number=values[0].page_number,
                heading_path=values[0].heading_path,
                anchor_ids=[item.anchor_id for item in values],
            )
            for _block_id, values in sorted(
                grouped.items(),
                key=lambda item: (item[1][0].block_no, item[0]),
            )
        ]

    def _finalize_context(self, value: RiskReviewContext, plan_id: str) -> RiskReviewContext:
        with_plan = value.model_copy(update={"plan_id": plan_id})
        payload = with_plan.model_dump(mode="json", exclude={"context_hash"})
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return with_plan.model_copy(update={"context_hash": "sha256:" + digest})

    def _unit_spec(
        self,
        *,
        unit_id: RiskDomain,
        checks: tuple[CheckSpec, ...],
        batches: list[ReviewBatchSpec],
    ) -> ReviewUnitSpec:
        horizontal = unit_id in HORIZONTAL_UNIT_IDS
        soft_limit, hard_limit = self._input_limits(unit_id)
        return ReviewUnitSpec(
            unit_id=unit_id,
            unit_type=ReviewUnitType.HORIZONTAL if horizontal else ReviewUnitType.BASE,
            domain=unit_id,
            required=True,
            check_specs=list(checks),
            required_ir_types=list(self._ir_types(checks)),
            ir_projection_fields=list(self._ir_types(checks)),
            source_selection_policy="CHECK_IR_TYPES_AND_ANCHORS",
            evidence_policy_id="contract-evidence-v1",
            deterministic_validator_ids=[
                "source-anchor-v1",
                "perspective-v1",
                "check-coverage-v1",
            ],
            model_id="contract-risk-direct-v1",
            temperature=0,
            thinking_enabled=False,
            max_repairs=1,
            timeout_seconds=30,
            target_input_tokens_min=1000 if horizontal else 2000,
            target_input_tokens_max=3000 if horizontal else 4000,
            soft_input_token_limit=soft_limit,
            hard_input_token_limit=hard_limit,
            target_output_token_limit=1500,
            soft_output_token_limit=2500,
            hard_output_token_limit=4000,
            requires_model=bool(batches),
            batch_ids=[item.batch_id for item in batches],
        )

    @staticmethod
    def _input_limits(unit_id: RiskDomain) -> tuple[int, int]:
        return (4000, 5000) if unit_id in HORIZONTAL_UNIT_IDS else (5000, 6000)

    def _estimate_for_checks(
        self,
        value: RiskReviewPlanInput,
        checks: tuple[CheckSpec, ...],
        all_items: tuple[RiskProjectedIrItem, ...],
        excerpts: dict[str, RiskSourceExcerpt],
        candidates: tuple[RiskHorizontalCandidate, ...],
    ) -> int:
        ir_types = self._ir_types(checks)
        projected = self._select_items(all_items, ir_types, candidates)
        selected_excerpts = self._select_excerpts(projected, excerpts, candidates)
        return self._estimate_context(value, checks, projected, selected_excerpts, candidates)

    @staticmethod
    def _estimate_context(
        value: RiskReviewPlanInput,
        checks: tuple[CheckSpec, ...],
        projected: tuple[RiskProjectedIrItem, ...],
        excerpts: tuple[RiskSourceExcerpt, ...],
        candidates: tuple[RiskHorizontalCandidate, ...],
    ) -> int:
        # Budget the exact semantic projection intended for the Direct Reviewer.
        # IDs, offsets and hashes remain in RiskReviewContext for deterministic
        # validation, but are not repeated in the model prompt. The model only
        # sees Anchor IDs plus literal source text and returns those IDs; Python
        # materializes all technical Evidence fields later.
        anchor_refs = {
            item.anchor_id: f"A{index:03d}"
            for index, item in enumerate(excerpts, start=1)
        }
        projected_groups: dict[IrField, list[dict[str, object]]] = defaultdict(list)
        include_item_id = bool(candidates)
        for item in projected:
            projected_item: dict[str, object] = {
                "predicate": item.predicate,
                "evidence_refs": [anchor_refs[anchor.anchor_id] for anchor in item.source_anchors],
            }
            if item.subject is not None:
                projected_item["subject"] = item.subject
            if item.object is not None:
                projected_item["object"] = item.object
            if include_item_id:
                projected_item["item_id"] = item.item_id
            projected_groups[item.ir_type].append(projected_item)

        payload = {
            "perspective": value.perspective.value,
            "our_party": value.our_party,
            "counterparty": value.counterparty,
            "contract_type": value.contract_type,
            "review_attitude": value.review_attitude,
            "checks": [
                {
                    "check_code": item.check_code,
                    "review_question": item.review_question,
                    "allowed_categories": [value.value for value in item.allowed_categories],
                    "allowed_risk_types": item.allowed_risk_types,
                }
                for item in checks
            ],
            "projected_ir": projected_groups,
            "source_excerpts": [
                {
                    "evidence_ref": anchor_refs[item.anchor_id],
                    "quoted_text": item.quoted_text,
                    **({"heading_path": item.heading_path} if item.heading_path else {}),
                }
                for item in excerpts
            ],
            "horizontal_candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "candidate_type": item.candidate_type,
                    "check_code": item.check_code,
                    "item_ids": item.item_ids,
                    "evidence_refs": [
                        anchor_refs[anchor_id]
                        for anchor_id in item.anchor_ids
                        if anchor_id in anchor_refs
                    ],
                    "reason_code": item.reason_code,
                }
                for item in candidates
            ],
        }
        text = canonical_json(payload)
        weighted = sum(1.0 if ord(character) > 127 else 0.25 for character in text)
        return max(1, math.ceil(weighted))

    @staticmethod
    def _ir_types(checks: Iterable[CheckSpec]) -> tuple[IrField, ...]:
        return tuple(
            dict.fromkeys(ir_type for check in checks for ir_type in check.required_ir_types)
        )

    @staticmethod
    def _select_items(
        all_items: tuple[RiskProjectedIrItem, ...],
        ir_types: tuple[IrField, ...],
        candidates: tuple[RiskHorizontalCandidate, ...],
    ) -> tuple[RiskProjectedIrItem, ...]:
        candidate_item_ids = {item_id for candidate in candidates for item_id in candidate.item_ids}
        if candidates and candidate_item_ids:
            return tuple(item for item in all_items if item.item_id in candidate_item_ids)
        return tuple(item for item in all_items if item.ir_type in ir_types)

    @staticmethod
    def _select_excerpts(
        items: tuple[RiskProjectedIrItem, ...],
        excerpts: dict[str, RiskSourceExcerpt],
        candidates: tuple[RiskHorizontalCandidate, ...] = (),
    ) -> tuple[RiskSourceExcerpt, ...]:
        anchor_ids = {
            anchor.anchor_id
            for item in items
            for anchor in item.source_anchors
        }
        anchor_ids.update(
            anchor_id
            for candidate in candidates
            for anchor_id in candidate.anchor_ids
        )
        return tuple(
            sorted(
                (excerpts[anchor_id] for anchor_id in anchor_ids),
                key=lambda item: (item.block_no, item.char_start, item.anchor_id),
            )
        )

    @staticmethod
    def _stable_id(prefix: str, payload: object) -> str:
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]
        return f"{prefix}-{digest}"

    def _plan_hash(
        self,
        *,
        value: RiskReviewPlanInput,
        selected_playbook_ids: list[str],
        units: list[ReviewUnitSpec],
        contexts: list[RiskReviewContext],
        deterministic_checks: list[DeterministicCheckResult],
        specialist_count: int,
    ) -> str:
        payload = {
            "plan_version": "1.0",
            "review_id": value.review_id,
            "document_id": value.document_id,
            "generation_id": value.generation_id,
            "attempt_no": value.attempt_no,
            "perspective": value.perspective.value,
            "contract_type": value.contract_type,
            "review_attitude": value.review_attitude,
            "selected_playbook_ids": selected_playbook_ids,
            "review_units": [item.model_dump(mode="json") for item in units],
            "contexts": [
                item.model_dump(mode="json", exclude={"plan_id", "context_hash"})
                for item in contexts
            ],
            "deterministic_checks": [
                item.model_dump(mode="json")
                for item in sorted(deterministic_checks, key=lambda result: result.check_code)
            ],
            "specialist_reviewer_count": specialist_count,
            "max_concurrency": 7,
            "required_unit_ids": list(BASE_UNIT_IDS),
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return "sha256:" + digest
