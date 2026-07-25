from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroundedConversationMessage(StrictModel):
    role: Literal["USER", "ASSISTANT"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class GroundedReportRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    document_id: Identifier
    instruction: Annotated[str, StringConstraints(max_length=4000)] | None = None


class GroundedChatRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    document_id: Identifier
    question: Annotated[str, StringConstraints(min_length=1, max_length=8000)]
    conversation_history: list[GroundedConversationMessage] = Field(
        default_factory=list,
        max_length=20,
    )


class GroundedReferenceData(StrictModel):
    reference_id: Annotated[
        str,
        StringConstraints(pattern=r"^docref-[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"),
    ]
    label: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    evidence_id: Identifier
    finding_id: Identifier
    document_id: Identifier
    contract_version_id: Identifier
    chunk_id: Identifier
    block_id: Identifier
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: Annotated[str, StringConstraints(min_length=1)]
    quoted_text_hash: Annotated[
        str,
        StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]


class GroundedAnswerData(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["REPORT", "CHAT"]
    review_id: Identifier
    document_id: Identifier
    contract_version_id: Identifier
    result_hash: Annotated[
        str,
        StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    content_markdown: Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
    references: list[GroundedReferenceData] = Field(default_factory=list, max_length=500)
