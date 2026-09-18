"""Pure, pluggable audit runtime. A pending/failed check never means a pass."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Callable, Mapping, Any, Literal

Status = Literal["PASS", "RISK", "PENDING", "ERROR"]


@dataclass(frozen=True)
class Evidence:
    source_id: str
    document_id: str
    document_version: str
    block_id: str
    page_number: int | None
    char_start: int
    char_end: int
    quoted_text: str
    quoted_text_hash: str


class EvidenceCatalog:
    """Backend builds from parsed blocks. Models select IDs, never supply coordinates."""
    def __init__(self, document_id: str, document_version: str, blocks: list[dict]):
        if not document_id or not document_version:
            raise ValueError("Document identity and version are required")
        items: dict[str, Evidence] = {}
        for block in blocks:
            key, text = block["source_id"], block["text"]
            if not key or key in items or not isinstance(text, str) or not text.strip():
                raise ValueError("Evidence IDs and source text must be valid and unique")
            items[key] = Evidence(key, document_id, document_version, block["block_id"],
                                  block.get("page_number"), 0, len(text), text,
                                  sha256(text.encode("utf-8")).hexdigest())
        self.document_id, self.document_version = document_id, document_version
        self._items = MappingProxyType(items)

    def select(self, refs: list[str], *, document_version: str,
               exposed_ids: frozenset[str]) -> tuple[Evidence, ...]:
        if document_version != self.document_version:
            raise ValueError("Evidence belongs to another document version")
        if len(refs) != len(set(refs)):
            raise ValueError("Duplicate evidence reference")
        if any(ref not in self._items or ref not in exposed_ids for ref in refs):
            raise ValueError("Unknown or unprovided evidence reference")
        return tuple(self._items[ref] for ref in refs)


@dataclass(frozen=True)
class AuditRecord:
    check_code: str
    status: Status
    message: str
    subject_id: str | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditContext:
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditResult:
    records: tuple[AuditRecord, ...]

    @property
    def status(self) -> str:
        if not self.records or any(r.status in {"PENDING", "ERROR"} for r in self.records):
            return "PARTIAL"
        return "RISK" if any(r.status == "RISK" for r in self.records) else "PASS"

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


class AuditRunner:
    def __init__(self):
        self._checks: dict[str, Callable[[AuditContext], list[AuditRecord]]] = {}

    def register(self, code: str, check: Callable[[AuditContext], list[AuditRecord]]) -> None:
        if not code or code in self._checks:
            raise ValueError("Check code must be unique")
        self._checks[code] = check

    def run(self, context: AuditContext) -> AuditResult:
        records: list[AuditRecord] = []
        for code, check in self._checks.items():
            try:
                rows = check(context)
                if not rows:
                    rows = [AuditRecord(code, "PENDING", "检查未返回结果")]
                if any(r.check_code != code or r.status not in {"PASS", "RISK", "PENDING", "ERROR"}
                       or not r.message for r in rows):
                    raise ValueError("Invalid audit record")
                records.extend(rows)
            except Exception:
                # Do not leak exception contents or suppress valid sibling checks.
                records.append(AuditRecord(code, "ERROR", "本项检查未完成，需要重试或人工复核"))
        return AuditResult(tuple(records))
