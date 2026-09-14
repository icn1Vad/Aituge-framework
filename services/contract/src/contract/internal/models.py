from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from contract.api.models import ReviewResultData, StrictModel
from contract.ir.models import ContractIR
from contract.risk.models import RiskReviewPlan
from contract.risk.models import RiskReviewPlanInput
from contract.legal_evidence.models import LegalEvidenceBundle


class ContractDocumentToolRequest(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)


class ContractBlocksToolRequest(ContractDocumentToolRequest):
    block_ids: list[str] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=200, ge=1, le=2000)


class ContractClauseContextToolRequest(ContractDocumentToolRequest):
    block_id: str = Field(min_length=1, max_length=160)
    before: int = Field(default=1, ge=0, le=5)
    after: int = Field(default=1, ge=0, le=5)


class ContractIrToolRequest(ContractDocumentToolRequest):
    pass


class ContractReviewResultToolRequest(ContractDocumentToolRequest):
    pass


class ContractWindowPlanToolRequest(ContractDocumentToolRequest):
    pass


class ContractRiskPlanRequest(ContractDocumentToolRequest):
    selected_playbook_ids: list[str] = Field(
        default_factory=lambda: ["base_neutral"],
        min_length=1,
    )


class ContractLegalEvidenceRequest(StrictModel):
    policy: Literal["OFF", "OPTIONAL", "REQUIRED"] = "OFF"
    plan_input: RiskReviewPlanInput


class ContractLegalEvidenceToolData(StrictModel):
    policy: Literal["OFF", "OPTIONAL", "REQUIRED"]
    enabled: bool
    usable: bool
    degradation_reasons: list[str] = Field(default_factory=list)
    bundle: LegalEvidenceBundle | None = None

    @model_validator(mode="after")
    def validate_policy_outcome(self) -> ContractLegalEvidenceToolData:
        if self.policy == "OFF":
            if self.enabled or self.usable or self.bundle is not None:
                raise ValueError("OFF legal-evidence policy must not execute a provider")
        elif not self.enabled:
            raise ValueError("OPTIONAL and REQUIRED legal-evidence policies are enabled")
        expected_usable = self.bundle is not None and self.bundle.usable
        if self.usable != expected_usable:
            raise ValueError("usable must match the returned legal-evidence bundle")
        if not self.usable and not self.degradation_reasons:
            raise ValueError("An unusable legal-evidence outcome requires a reason")
        return self


class ContractDocumentToolData(StrictModel):
    review_id: str
    document_id: str
    contract_version_id: str
    original_name: str
    content_type: str
    file_type: Literal["pdf", "docx"]
    file_size: int = Field(gt=0)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_id: str
    generation_status: Literal["RUNNING", "SUCCEEDED"]
    block_count: int = Field(gt=0)


class ContractBlockData(StrictModel):
    block_id: str
    block_no: int = Field(gt=0)
    block_type: str
    page_number: int | None = Field(default=None, ge=1)
    paragraph_no: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    text: str = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ContractBlocksToolData(StrictModel):
    review_id: str
    document_id: str
    generation_id: str
    blocks: list[ContractBlockData]


class ContractClauseContextToolData(ContractBlocksToolData):
    target_block_id: str


class ContractIrToolData(StrictModel):
    review_id: str
    document_id: str
    generation_id: str
    generation_status: Literal["RUNNING", "SUCCEEDED"]
    contract_ir: ContractIR


class ContractReviewResultToolData(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    result: ReviewResultData


class ContractWindowExpectedBlockData(StrictModel):
    block_id: str = Field(min_length=1, max_length=160)
    text_length: int = Field(gt=0)


class ContractWindowOffsetData(StrictModel):
    rendered_start: int = Field(ge=0)
    rendered_end: int = Field(gt=0)
    block_id: str = Field(min_length=1, max_length=160)
    block_no: int = Field(ge=1)
    block_char_start: int = Field(ge=0)
    block_char_end: int = Field(gt=0)
    page_number: int | None = Field(default=None, ge=1)


class ContractWindowData(StrictModel):
    window_id: str = Field(min_length=1, max_length=160)
    sequence_no: int = Field(ge=1)
    section_ids: list[str] = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    clause_nos: list[str] = Field(default_factory=list)
    primary_block_ids: list[str] = Field(min_length=1)
    estimated_tokens: int = Field(ge=1)
    source_text: str = Field(min_length=1)
    context_text: str = Field(default="")
    offset_map: list[ContractWindowOffsetData] = Field(min_length=1)


class ContractWindowPlanToolData(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    expected_blocks: list[ContractWindowExpectedBlockData] = Field(min_length=1)
    expected_section_ids: list[str] = Field(min_length=1)
    windows: list[ContractWindowData] = Field(min_length=1)
    concurrency: Literal[10] = 10


ContractRiskPlanData = RiskReviewPlan
