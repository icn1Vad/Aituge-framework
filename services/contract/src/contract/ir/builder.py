from __future__ import annotations

import hashlib
import re

from contract.ir.models import ContractIR, IRClause, IRDocument, SourceAnchor
from contract.parser.models import ParsedContract, ParsedContractBlock


_CLAUSE_NUMBER = re.compile(r"^[\s\u3000]*(第[\s\u3000]*[〇零一二三四五六七八九十百千万两0-9０-９]+[\s\u3000]*条)")


def build_structural_contract_ir(
    parsed: ParsedContract,
    *,
    document_id: str,
    generation_id: str,
    content_hash: str,
    parser_version: str,
) -> ContractIR:
    anchors = [_anchor_for(block) for block in parsed.blocks if block.block_type != "footer"]
    clauses = [
        IRClause(
            clause_id=_stable_id("clause", generation_id, block.block_id),
            clause_no=_clause_number(block.text),
            clause_type=block.block_type,
            heading_path=list(block.heading_path),
            text=block.text,
            source_anchors=[_anchor_for(block)],
        )
        for block in parsed.blocks
        if block.block_type != "footer"
    ]
    if not anchors or not clauses:
        raise ValueError("Contract IR requires at least one non-footer source block")
    return ContractIR(
        document=IRDocument(
            document_id=document_id,
            generation_id=generation_id,
            content_hash=content_hash,
            file_type=parsed.file_type,
            page_count=parsed.page_count,
            block_count=len(parsed.blocks),
            parser_version=parser_version,
            warnings=list(parsed.warnings),
        ),
        clauses=clauses,
        source_anchors=anchors,
    )


def _anchor_for(block: ParsedContractBlock) -> SourceAnchor:
    return SourceAnchor(
        anchor_id=_stable_id("anchor", block.block_id, "0", str(len(block.text))),
        block_id=block.block_id,
        page_number=block.page_number,
        char_start=0,
        char_end=len(block.text),
    )


def _clause_number(text: str) -> str | None:
    matched = _CLAUSE_NUMBER.match(text)
    return matched.group(1).replace(" ", "").replace("\u3000", "") if matched else None


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
