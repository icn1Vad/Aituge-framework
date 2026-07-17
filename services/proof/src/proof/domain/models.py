from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DocumentBlock:
    id: str
    ordinal: int
    block_type: str
    text: str
    page_no: int | None = None
    paragraph_index: int | None = None
    heading_path: list[str] = field(default_factory=list)
    char_start: int | None = None
    char_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedDocument:
    blocks: list[DocumentBlock]
    file_type: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalUnit:
    id: str
    clause_no_raw: str
    clause_ordinal: int
    text: str
    heading_path: list[str]
    source_block_ids: list[str]
    page_start: int | None
    page_end: int | None
    paragraph_start: int | None
    paragraph_end: int | None
    char_start: int | None
    char_end: int | None
    text_hash: str
    unit_type: str = "article"


@dataclass(slots=True)
class StructureExtractionResult:
    units: list[RetrievalUnit]
    profile: str
    profiles_detected: list[str]
    regions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IngestionResult:
    policy: dict[str, Any]
    document: dict[str, Any]
    clauses: list[dict[str, Any]]
    reused: bool = False
