from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from contract.api.models import StrictModel
from contract.ir.models import ContractIR


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
