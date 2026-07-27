from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsedContractBlock:
    block_id: str
    block_no: int
    block_type: str
    text: str
    page_number: int | None
    paragraph_no: int | None
    char_start: int
    char_end: int
    heading_path: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedContract:
    file_type: str
    blocks: list[ParsedContractBlock]
    page_count: int | None
    warnings: list[str] = field(default_factory=list)
