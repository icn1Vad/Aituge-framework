from __future__ import annotations

from collections import Counter
from typing import Any

from contract.api.models import ContractProfile, Evidence, Finding, ReviewResultData, ReviewSummary
from contract.application.result_hash import compute_result_hash
from contract.callback.models import (
    CommercialTermsStageResult,
    EvidenceVerificationStageResult,
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
from contract.errors import ContractError
from contract.internal.models import (
    ContractBlockData,
    ContractBlocksToolData,
    ContractBlocksToolRequest,
    ContractClauseContextToolData,
    ContractClauseContextToolRequest,
    ContractDocumentToolData,
    ContractDocumentToolRequest,
    ContractIrToolData,
    ContractIrToolRequest,
)
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.repository import ContractRepository


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
    ) -> None:
        self.repository = repository
        self.callback_repository = callback_repository

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
        return ContractIrToolData(
            review_id=review["id"],
            document_id=review["document_id"],
            generation_id=generation["id"],
            generation_status=generation["status"],
            contract_ir=generation["contract_ir_json"],
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
        self._validate_evidence(review, validated.findings, validated.evidences)
        return validated

    def _execution_context(self, request: StageExecuteRequest) -> dict[str, Any]:
        review = self.callback_repository.get_review_context(request.review_id)
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
        task = request.task_input
        if (
            task.business_task_id != review["business_task_id"]
            or task.contract_version_id != review["contract_version_id"]
            or task.document_id != review["document_id"]
            or task.perspective != review["perspective"]
            or task.review_attitude != review["review_attitude"]
        ):
            raise ContractError(
                "FRAMEWORK_CALLBACK_MISMATCH",
                "Framework task input does not match the contract review",
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
        findings, evidences = self._merge_review_artifacts(request.artifacts)
        self._validate_evidence(review, findings, evidences)
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
    ) -> tuple[list[Finding], list[Evidence]]:
        chosen: dict[tuple[Any, ...], dict[str, Any]] = {}
        id_mapping: dict[str, str] = {}
        evidence_values: list[dict[str, Any]] = []
        for artifact_type, model in REVIEW_ARTIFACT_MODELS.items():
            raw = artifacts.get(artifact_type)
            if raw is None:
                raise ContractError(
                    "RESULT_INVALID",
                    f"Required review artifact '{artifact_type}' is missing",
                    status_code=422,
                )
            stage = model.model_validate(raw)
            for finding in stage.findings:
                value = finding.model_dump(mode="json")
                key = (
                    value["category"],
                    value["risk_level"],
                    value["title"].strip(),
                    value["issue"].strip(),
                    value["impact_to_our_party"].strip(),
                    value["suggestion"].strip(),
                    value["perspective"],
                    value["our_party"],
                    value["counterparty"],
                )
                current = chosen.get(key)
                if current is None:
                    chosen[key] = value
                    id_mapping[value["finding_id"]] = value["finding_id"]
                else:
                    id_mapping[value["finding_id"]] = current["finding_id"]
            evidence_values.extend(item.model_dump(mode="json") for item in stage.evidences)

        findings_by_id = {value["finding_id"]: value for value in chosen.values()}
        for value in findings_by_id.values():
            value["evidence_ids"] = []
        evidences_by_id: dict[str, dict[str, Any]] = {}
        for evidence in evidence_values:
            mapped = id_mapping.get(evidence["finding_id"])
            if mapped is None:
                raise ContractError("RESULT_INVALID", "Evidence references an unknown finding", status_code=422)
            evidence["finding_id"] = mapped
            existing = evidences_by_id.get(evidence["evidence_id"])
            if existing is not None and existing != evidence:
                raise ContractError(
                    "RESULT_INVALID",
                    "Evidence ID is duplicated with different content",
                    status_code=422,
                )
            evidences_by_id[evidence["evidence_id"]] = evidence
            evidence_ids = findings_by_id[mapped]["evidence_ids"]
            if evidence["evidence_id"] not in evidence_ids:
                evidence_ids.append(evidence["evidence_id"])
        findings = [Finding.model_validate(value) for value in findings_by_id.values()]
        evidences = [Evidence.model_validate(value) for value in evidences_by_id.values()]
        return findings, evidences

    def _validate_evidence(
        self,
        review: dict[str, Any],
        findings: list[Finding],
        evidences: list[Evidence],
    ) -> None:
        generation = self.repository.get_active_generation(
            review["document_id"],
            tenant_id=review["tenant_id"],
        )
        if generation is None:
            raise ContractError("EVIDENCE_INVALID", "Active Contract IR is not available", status_code=422)
        blocks = {
            block["block_id"]: block
            for block in self.repository.list_blocks(generation["id"], tenant_id=review["tenant_id"])
        }
        finding_by_id = {finding.finding_id: finding for finding in findings}
        evidence_by_id = {evidence.evidence_id: evidence for evidence in evidences}
        for finding in findings:
            if (
                finding.perspective.value != review["perspective"]
                or not finding.evidence_ids
            ):
                raise ContractError("EVIDENCE_INVALID", "Finding perspective or evidence is invalid", status_code=422)
            for evidence_id in finding.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None or evidence.finding_id != finding.finding_id:
                    raise ContractError("EVIDENCE_INVALID", "Finding evidence link is invalid", status_code=422)
        for evidence in evidences:
            if evidence.finding_id not in finding_by_id:
                raise ContractError("EVIDENCE_INVALID", "Evidence finding does not exist", status_code=422)
            if evidence.evidence_type.value == "ABSENCE":
                continue
            block = blocks.get(evidence.block_id or "")
            if block is None:
                raise ContractError("EVIDENCE_INVALID", "Evidence block is outside this contract", status_code=422)
            assert evidence.char_start is not None and evidence.char_end is not None
            assert evidence.quoted_text is not None
            if block["text"][evidence.char_start : evidence.char_end] != evidence.quoted_text:
                raise ContractError("EVIDENCE_INVALID", "Evidence text does not match its block", status_code=422)
            if evidence.page_number is not None and evidence.page_number != block["page_number"]:
                raise ContractError("EVIDENCE_INVALID", "Evidence page does not match its block", status_code=422)

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
        return ContractBlockData(
            block_id=value["block_id"],
            block_no=value["block_no"],
            block_type=value["block_type"],
            page_number=value["page_number"],
            paragraph_no=value["paragraph_no"],
            char_start=value["char_start"],
            char_end=value["char_end"],
            text=value["text"],
            heading_path=value["heading_path"],
            metadata=value["metadata_json"],
        )
