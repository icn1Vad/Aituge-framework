from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class StrictIRModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceAnchor(StrictIRModel):
    anchor_id: NonEmptyText
    block_id: NonEmptyText
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "SourceAnchor":
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class IRDocument(StrictIRModel):
    document_id: NonEmptyText
    generation_id: NonEmptyText
    content_hash: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    file_type: Literal["pdf", "docx"]
    page_count: int | None = Field(default=None, ge=1)
    block_count: int = Field(gt=0)
    parser_version: NonEmptyText
    warnings: list[str] = Field(default_factory=list)


class IRParty(StrictIRModel):
    role: Literal["PARTY_A", "PARTY_B", "OTHER"]
    name: NonEmptyText
    name_resolved: bool = True
    source_anchors: list[SourceAnchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_source(self) -> "IRParty":
        if self.name_resolved and not self.source_anchors:
            raise ValueError("Resolved party names require at least one source anchor")
        placeholder_by_role = {"PARTY_A": "甲方", "PARTY_B": "乙方"}
        if not self.name_resolved:
            expected = placeholder_by_role.get(self.role)
            if expected is None or self.name != expected:
                raise ValueError(
                    "Unresolved party names must use the canonical role placeholder"
                )
        return self


class IRDefinition(StrictIRModel):
    term: NonEmptyText
    meaning: NonEmptyText
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class IRClause(StrictIRModel):
    clause_id: NonEmptyText
    clause_no: str | None = None
    clause_type: NonEmptyText
    heading_path: list[str] = Field(default_factory=list)
    text: NonEmptyText
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class IRSemanticItem(StrictIRModel):
    item_id: NonEmptyText
    subject: str | None = None
    predicate: NonEmptyText
    object: str | None = None
    source_anchors: list[SourceAnchor] = Field(min_length=1)


class ContractIR(StrictIRModel):
    ir_version: Literal["1.0"] = "1.0"
    document: IRDocument
    parties: list[IRParty] = Field(default_factory=list)
    our_party: NonEmptyText | None = None
    counterparty: NonEmptyText | None = None
    contract_type: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")] = "AUTO"
    definitions: list[IRDefinition] = Field(default_factory=list)
    clauses: list[IRClause] = Field(min_length=1)
    rights: list[IRSemanticItem] = Field(default_factory=list)
    obligations: list[IRSemanticItem] = Field(default_factory=list)
    prohibitions: list[IRSemanticItem] = Field(default_factory=list)
    payment_terms: list[IRSemanticItem] = Field(default_factory=list)
    delivery_terms: list[IRSemanticItem] = Field(default_factory=list)
    acceptance_terms: list[IRSemanticItem] = Field(default_factory=list)
    liabilities: list[IRSemanticItem] = Field(default_factory=list)
    termination_terms: list[IRSemanticItem] = Field(default_factory=list)
    confidentiality_terms: list[IRSemanticItem] = Field(default_factory=list)
    intellectual_property_terms: list[IRSemanticItem] = Field(default_factory=list)
    dispute_resolution: list[IRSemanticItem] = Field(default_factory=list)
    dates: list[IRSemanticItem] = Field(default_factory=list)
    amounts: list[IRSemanticItem] = Field(default_factory=list)
    source_anchors: list[SourceAnchor] = Field(min_length=1)
