from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from contract.api.models import ContractProfile, Evidence, Finding, ReviewResultData, ReviewSummary
from contract.application.result_hash import compute_result_hash
from contract.callback.models import (
    CommercialTermsStageResult,
    EvidenceCandidate,
    EvidenceVerificationStageResult,
    ExtractContractIrStageResult,
    FindingConsolidationArtifact,
    FinalizeReviewStageResult,
    FrameworkTaskInput,
    LiabilityTerminationStageResult,
    MissingAmbiguityStageResult,
    ParseContractStageResult,
    PartyResolutionStageResult,
    RelationExtractionStageResult,
    RightsObligationsStageResult,
    StageExecuteRequest,
)
from contract.evidence import materialize_evidence_set, validate_evidence_set
from contract.errors import ContractError
from contract.internal.models import (
    ContractBlockData,
    ContractBlocksToolData,
    ContractBlocksToolRequest,
    ContractClauseContextToolData,
    ContractClauseContextToolRequest,
    ContractRiskPlanRequest,
    ContractDocumentToolData,
    ContractDocumentToolRequest,
    ContractIrToolData,
    ContractIrToolRequest,
    ContractReviewResultToolData,
    ContractReviewResultToolRequest,
    ContractWindowData,
    ContractWindowExpectedBlockData,
    ContractWindowOffsetData,
    ContractWindowPlanToolData,
    ContractWindowPlanToolRequest,
)
from contract.risk.models import RiskReviewPlan, RiskReviewPlanInput, RiskSourceBlock
from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.ir.windowing import build_section_units, build_section_windows, validate_window_coverage
from contract.parser.models import ParsedContractBlock
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.repository import ContractRepository
from contract.review import merge_review_stage_results, namespace_review_stage_result


REVIEW_ARTIFACT_MODELS = {
    "rights_obligations_review_result": RightsObligationsStageResult,
    "commercial_terms_review_result": CommercialTermsStageResult,
    "liability_termination_review_result": LiabilityTerminationStageResult,
    "missing_ambiguous_clauses_result": MissingAmbiguityStageResult,
    "relation_extraction_result": RelationExtractionStageResult,
}


class ContractInternalService:
    def __init__(
        self,
        repository: ContractRepository,
        callback_repository: FrameworkCallbackRepository,
        risk_plan_builder: RiskReviewPlanBuilder | None = None,
    ) -> None:
        self.repository = repository
        self.callback_repository = callback_repository
        self.risk_plan_builder = risk_plan_builder or RiskReviewPlanBuilder()

    def execute_stage(self, request: StageExecuteRequest):
        context = self._execution_context(request)
        if request.stage_id == "parse_contract":
            generation = self._generation(context)
            return ParseContractStageResult(
                result_type="PARSE_CONTRACT_STAGE_V1",
                document_id=context["document_id"],
                generation_id=generation["id"],
                block_count=generation["block_count"],
                ir_hash=generation["ir_hash"],
            )
        if request.stage_id == "verify_evidence":
            return self._verify_evidence(request, context)
        if request.stage_id == "finalize_review":
            return self._finalize(request, context)
        raise ContractError("INVALID_REQUEST", "Unsupported contract gateway stage", status_code=400)

    def get_document(self, request: ContractDocumentToolRequest) -> ContractDocumentToolData:
        review = self._tool_context(request.review_id, request.document_id)
        document = self.repository.get_document(
            review["document_id"],
            tenant_id=review["tenant_id"],
            user_id=review["user_id"],
        )
        if document is None:
            raise ContractError("REVIEW_NOT_FOUND", "Contract document does not exist", status_code=404)
        generation = self._generation(review)
        return ContractDocumentToolData(
            review_id=review["id"],
            document_id=document["id"],
            contract_version_id=document["contract_version_id"],
            original_name=document["original_name"],
            content_type=document["content_type"],
            file_type=document["file_type"],
            file_size=document["file_size"],
            content_hash=document["content_hash"],
            generation_id=generation["id"],
            generation_status=generation["status"],
            block_count=generation["block_count"],
        )

    def get_blocks(self, request: ContractBlocksToolRequest) -> ContractBlocksToolData:
        review = self._tool_context(request.review_id, request.document_id)
        generation = self._generation(review)
        blocks = self.repository.list_blocks(generation["id"], tenant_id=review["tenant_id"])
        selected = set(request.block_ids)
        if selected:
            blocks = [block for block in blocks if block["block_id"] in selected]
            missing = selected - {block["block_id"] for block in blocks}
            if missing:
                raise ContractError(
                    "RESULT_INVALID",
                    "Requested contract block does not belong to the current document",
                    status_code=422,
                    details={"missing_block_ids": sorted(missing)},
                )
        return ContractBlocksToolData(
            review_id=review["id"],
            document_id=review["document_id"],
            generation_id=generation["id"],
            blocks=[self._block(block) for block in blocks[: request.limit]],
        )

    def get_clause_context(
        self,
        request: ContractClauseContextToolRequest,
    ) -> ContractClauseContextToolData:
        review = self._tool_context(request.review_id, request.document_id)
        generation = self._generation(review)
        blocks = self.repository.list_blocks(generation["id"], tenant_id=review["tenant_id"])
        index = next((i for i, item in enumerate(blocks) if item["block_id"] == request.block_id), None)
        if index is None:
            raise ContractError("RESULT_INVALID", "Contract block does not exist", status_code=422)
        selected = blocks[max(0, index - request.before) : index + request.after + 1]
        return ContractClauseContextToolData(
            review_id=review["id"],
            document_id=review["document_id"],
            generation_id=generation["id"],
            target_block_id=request.block_id,
            blocks=[self._block(block) for block in selected],
        )

    def get_ir(self, request: ContractIrToolRequest) -> ContractIrToolData:
        review = self._tool_context(request.review_id, request.document_id)
        generation = self._generation(review)
        if not isinstance(generation["contract_ir_json"], dict):
            raise ContractError("RESULT_INVALID", "Contract IR is not available", status_code=422)
        contract_ir = copy.deepcopy(generation["contract_ir_json"])
        attempt_no = review.get("active_attempt_no")
        if attempt_no is not None:
            party = self.callback_repository.get_validated_stage_result(
                review["id"], attempt_no, "resolve_parties"
            )
            if party is not None:
                contract_ir["contract_type"] = party["contract_type"]
                contract_ir["our_party"] = party["our_party"]
                contract_ir["counterparty"] = party["counterparty"]
        return ContractIrToolData(
            review_id=review["id"],
            document_id=review["document_id"],
            generation_id=generation["id"],
            generation_status=generation["status"],
            contract_ir=contract_ir,
        )

    def get_review_result(
        self,
        request: ContractReviewResultToolRequest,
    ) -> ContractReviewResultToolData:
        review = self._tool_context(request.review_id, request.document_id)
        snapshot = self.callback_repository.get_revision_source_snapshot(
            review["id"],
            tenant_id=review["tenant_id"],
            user_id=review["user_id"],
        )
        if snapshot["review_status"] != "SUCCEEDED":
            raise ContractError(
                "RESULT_NOT_READY",
                "Contract review result is not ready",
                status_code=409,
            )
        result = snapshot.get("result_json")
        generation_id = snapshot.get("generation_id")
        if not isinstance(result, dict) or not isinstance(generation_id, str):
            raise ContractError(
                "RESULT_INVALID",
                "Completed contract review result is unavailable",
                status_code=422,
            )
        return ContractReviewResultToolData(
            review_id=review["id"],
            document_id=review["document_id"],
            generation_id=generation_id,
            result=ReviewResultData.model_validate(result),
        )

    def get_window_plan(
        self,
        request: ContractWindowPlanToolRequest,
    ) -> ContractWindowPlanToolData:
        review = self._tool_context(request.review_id, request.document_id)
        generation = self._generation(review)
        rows = self.repository.list_blocks(generation["id"], tenant_id=review["tenant_id"])
        blocks = [
            ParsedContractBlock(
                block_id=row["block_id"],
                block_no=row["block_no"],
                block_type=row["block_type"],
                text=row["text"],
                page_number=row["page_number"],
                paragraph_no=row["paragraph_no"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                heading_path=list(row["heading_path"]),
                metadata=dict(row["metadata_json"]),
            )
            for row in rows
        ]
        sections = build_section_units(blocks)
        windows = build_section_windows(sections)
        validate_window_coverage(blocks, windows)
        expected = [item for item in blocks if item.block_type != "footer" and item.text]
        return ContractWindowPlanToolData(
            review_id=review["id"],
            document_id=review["document_id"],
            generation_id=generation["id"],
            expected_blocks=[
                ContractWindowExpectedBlockData(
                    block_id=item.block_id,
                    text_length=len(item.text),
                )
                for item in expected
            ],
            expected_section_ids=[item.section_id for item in sections],
            windows=[
                ContractWindowData(
                    window_id=window.window_id,
                    sequence_no=window.sequence_no,
                    section_ids=list(window.section_ids),
                    heading_path=list(window.heading_path),
                    clause_nos=list(window.clause_nos),
                    primary_block_ids=list(window.primary_block_ids),
                    estimated_tokens=window.estimated_tokens,
                    source_text=window.source_text,
                    context_text=window.context_text,
                    offset_map=[
                        ContractWindowOffsetData(
                            rendered_start=offset.rendered_start,
                            rendered_end=offset.rendered_end,
                            block_id=offset.block_id,
                            block_no=offset.block_no,
                            block_char_start=offset.block_char_start,
                            block_char_end=offset.block_char_end,
                            page_number=offset.page_number,
                        )
                        for offset in window.offset_map
                    ],
                )
                for window in windows
            ],
        )

    def get_risk_plan(self, request: ContractRiskPlanRequest) -> RiskReviewPlan:
        review = self._tool_context(request.review_id, request.document_id)
        generation = self._generation(review)
        if not isinstance(generation["contract_ir_json"], dict):
            raise ContractError("RESULT_INVALID", "Contract IR is not available", status_code=422)
        attempt_no = review.get("active_attempt_no")
        if not isinstance(attempt_no, int):
            raise ContractError("RESULT_INVALID", "Review Attempt is unavailable", status_code=422)
        party_value = self.callback_repository.get_validated_stage_result(
            review["id"],
            attempt_no,
            "resolve_parties",
        )
        if party_value is None:
            raise ContractError(
                "RESULT_INVALID",
                "Validated party resolution is unavailable",
                status_code=422,
            )
        party = PartyResolutionStageResult.model_validate(party_value)
        full_ir = ContractIR.model_validate(copy.deepcopy(generation["contract_ir_json"]))
        stage_result = ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            semantic_ir={
                field: getattr(full_ir, field)
                for field in (
                    "definitions",
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
                )
            },
        )
        rows = self.repository.list_blocks(generation["id"], tenant_id=review["tenant_id"])
        return self.risk_plan_builder.build(
            RiskReviewPlanInput(
                review_id=review["id"],
                document_id=review["document_id"],
                generation_id=generation["id"],
                attempt_no=attempt_no,
                perspective=party.perspective,
                our_party=party.our_party,
                counterparty=party.counterparty,
                contract_type=party.contract_type,
                review_attitude="NEUTRAL",
                stage_result=stage_result,
                source_blocks=[
                    RiskSourceBlock(
                        block_id=row["block_id"],
                        block_no=row["block_no"],
                        text=row["text"],
                        page_number=row["page_number"],
                        heading_path=list(row["heading_path"]),
                    )
                    for row in rows
                    if row["block_type"] != "footer" and row["text"]
                ],
                selected_playbook_ids=request.selected_playbook_ids,
            )
        )

    def validate_final_result(self, value: FinalizeReviewStageResult) -> ReviewResultData:
        review = self.callback_repository.get_review_context(value.review_id)
        if (
            value.business_task_id != review["business_task_id"]
            or value.contract_version_id != review["contract_version_id"]
            or value.contract_profile.perspective.value != review["perspective"]
        ):
            raise ContractError(
                "RESULT_INVALID",
                "Final contract result identity does not match the review",
                status_code=422,
            )
        raw = value.model_dump(mode="json")
        raw.pop("result_type", None)
        result_hash, _canonical = compute_result_hash(raw)
        raw["result_hash"] = result_hash
        validated = ReviewResultData.model_validate(raw)
        self._validate_evidence(
            review,
            validated.findings,
            validated.evidences,
            validated.contract_profile,
        )
        return validated

    def _execution_context(self, request: StageExecuteRequest) -> dict[str, Any]:
        review = self.callback_repository.get_stage_execution_context(request)
        attempt = review.get("active_attempt")
        if (
            review["status"] != "RUNNING"
            or review["active_attempt_no"] != request.attempt_no
            or attempt is None
            or attempt["framework_task_id"] != request.framework_task_id
            or attempt["framework_run_id"] != request.framework_run_id
            or not attempt["is_active"]
        ):
            raise ContractError(
                "FRAMEWORK_CALLBACK_MISMATCH",
                "Framework stage execution does not match the active Attempt",
                status_code=409,
            )
        return review

    def _verify_evidence(
        self,
        request: StageExecuteRequest,
        review: dict[str, Any],
    ) -> EvidenceVerificationStageResult:
        party_value = request.artifacts.get("contract_party_resolution")
        if party_value is None:
            raise ContractError("RESULT_INVALID", "Party resolution artifact is missing", status_code=422)
        party = PartyResolutionStageResult.model_validate(party_value)
        findings, evidence_candidates = self._merge_review_artifacts(request.artifacts)
        profile = ContractProfile(
            contract_type=party.contract_type,
            party_a=party.party_a,
            party_b=party.party_b,
            perspective=party.perspective,
            our_party=party.our_party,
            counterparty=party.counterparty,
            review_attitude="NEUTRAL",
        )
        if profile.perspective.value != review["perspective"]:
            raise ContractError("RESULT_INVALID", "Review perspective changed during execution", status_code=422)
        blocks = self._attempt_blocks(review)
        evidences = materialize_evidence_set(
            findings,
            evidence_candidates,
            blocks,
            profile,
        )
        overview = (
            f"发现{len(findings)}项需要人工复核的合同事项。"
            if findings
            else "未发现需要人工复核的实质合同风险。"
        )
        return EvidenceVerificationStageResult(
            result_type="EVIDENCE_VERIFICATION_STAGE_V1",
            contract_profile=profile,
            overview=overview,
            findings=findings,
            evidences=evidences,
            relationships=[],
        )

    @staticmethod
    def _finalize(
        request: StageExecuteRequest,
        review: dict[str, Any],
    ) -> FinalizeReviewStageResult:
        value = request.artifacts.get("contract_verified_findings")
        if value is None:
            raise ContractError("RESULT_INVALID", "Verified findings artifact is missing", status_code=422)
        verified = EvidenceVerificationStageResult.model_validate(value)
        counts = Counter(finding.risk_level.value for finding in verified.findings)
        return FinalizeReviewStageResult(
            result_type="FINAL_REVIEW_STAGE_V1",
            schema_version="1.0",
            review_id=review["id"],
            business_task_id=review["business_task_id"],
            contract_version_id=review["contract_version_id"],
            contract_profile=verified.contract_profile,
            summary=ReviewSummary(
                overview=verified.overview,
                high_count=counts["HIGH"],
                medium_count=counts["MEDIUM"],
                low_count=counts["LOW"],
                info_count=counts["INFO"],
            ),
            findings=verified.findings,
            evidences=verified.evidences,
            relationships=[],
        )

    @staticmethod
    def _merge_review_artifacts(
        artifacts: dict[str, dict[str, Any]],
    ) -> tuple[list[Finding], list[EvidenceCandidate]]:
        stages = []
        finding_reference_ids: dict[tuple[str, str], str] = {}
        for artifact_type, model in REVIEW_ARTIFACT_MODELS.items():
            raw = artifacts.get(artifact_type)
            if raw is None:
                raise ContractError(
                    "RESULT_INVALID",
                    f"Required review artifact '{artifact_type}' is missing",
                    status_code=422,
                )
            source_stage = model.model_validate(raw)
            namespaced_stage = namespace_review_stage_result(artifact_type, source_stage)
            stages.append(namespaced_stage)
            finding_reference_ids.update(
                {
                    (artifact_type, source.finding_id): namespaced.finding_id
                    for source, namespaced in zip(
                        source_stage.findings,
                        namespaced_stage.findings,
                        strict=True,
                    )
                }
            )
        raw_consolidation = artifacts.get("contract_finding_consolidation")
        consolidation = (
            FindingConsolidationArtifact.model_validate(raw_consolidation)
            if raw_consolidation is not None
            else None
        )
        return merge_review_stage_results(
            stages,
            consolidation=consolidation,
            finding_reference_ids=finding_reference_ids,
        )

    def _validate_evidence(
        self,
        review: dict[str, Any],
        findings: list[Finding],
        evidences: list[Evidence],
        profile: ContractProfile,
    ) -> None:
        validate_evidence_set(findings, evidences, self._attempt_blocks(review), profile)

    def _attempt_blocks(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        attempt_no = review.get("active_attempt_no")
        if not isinstance(attempt_no, int):
            raise ContractError("EVIDENCE_INVALID", "Review Attempt is unavailable", status_code=422)
        generation = self.callback_repository.get_attempt_parse_generation(
            review["id"],
            attempt_no,
        )
        if generation is None:
            raise ContractError(
                "EVIDENCE_INVALID",
                "Validated parse Generation is unavailable for this Attempt",
                status_code=422,
            )
        return self.repository.list_blocks(generation["id"], tenant_id=review["tenant_id"])

    def _tool_context(self, review_id: str, document_id: str) -> dict[str, Any]:
        review = self.callback_repository.get_review_context(review_id)
        if review["document_id"] != document_id:
            raise ContractError("ACCESS_DENIED", "Document is outside this contract review", status_code=403)
        return review

    def _generation(self, review: dict[str, Any]) -> dict[str, Any]:
        generation = review.get("generation") or self.repository.get_current_generation(
            review["document_id"],
            tenant_id=review["tenant_id"],
        )
        if generation is None or generation["block_count"] <= 0 or not generation["ir_hash"]:
            raise ContractError("RESULT_INVALID", "Contract parse generation is incomplete", status_code=422)
        return generation

    @staticmethod
    def _block(value: dict[str, Any]) -> ContractBlockData:
        text = value["text"]
        return ContractBlockData(
            block_id=value["block_id"],
            block_no=value["block_no"],
            block_type=value["block_type"],
            page_number=value["page_number"],
            paragraph_no=value["paragraph_no"],
            # Contract tools expose Evidence coordinates, which are always
            # relative to this Block. Database offsets remain document-relative
            # technical metadata and must not leak into the model contract.
            char_start=0,
            char_end=len(text),
            text=text,
            heading_path=value["heading_path"],
            metadata=value["metadata_json"],
        )
