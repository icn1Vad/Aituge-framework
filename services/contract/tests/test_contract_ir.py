from __future__ import annotations

import pytest
from pydantic import ValidationError

from contract.ir import build_structural_contract_ir
from contract.ir.models import SourceAnchor
from contract.parser.models import ParsedContract, ParsedContractBlock


def _block(block_no: int, text: str, block_type: str) -> ParsedContractBlock:
    return ParsedContractBlock(
        block_id=f"block-{block_no}",
        block_no=block_no,
        block_type=block_type,
        text=text,
        page_number=1,
        paragraph_no=block_no,
        char_start=0 if block_no == 1 else 10,
        char_end=(0 if block_no == 1 else 10) + len(text),
        heading_path=["第一章 总则"],
        metadata={},
    )


def test_structural_ir_has_all_frozen_sections_and_relative_anchors() -> None:
    parsed = ParsedContract(
        file_type="pdf",
        page_count=1,
        blocks=[
            _block(1, "第一章 总则", "heading"),
            _block(2, "第一条 甲方应付款。", "article"),
        ],
    )

    result = build_structural_contract_ir(
        parsed,
        document_id="document-1",
        generation_id="generation-1",
        content_hash="sha256:" + "a" * 64,
        parser_version="contract-parser-v1",
    )
    payload = result.model_dump(mode="json")

    assert result.clauses[1].clause_no == "第一条"
    assert result.clauses[1].source_anchors[0].char_start == 0
    assert result.clauses[1].source_anchors[0].char_end == len("第一条 甲方应付款。")
    assert result.document.block_count == 2
    for field in (
        "parties",
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
        "source_anchors",
    ):
        assert field in payload


def test_structural_ir_identity_is_stable() -> None:
    parsed = ParsedContract(
        file_type="docx",
        page_count=None,
        blocks=[_block(1, "合同正文", "paragraph")],
    )
    values = {
        "document_id": "document-1",
        "generation_id": "generation-1",
        "content_hash": "sha256:" + "b" * 64,
        "parser_version": "contract-parser-v1",
    }

    first = build_structural_contract_ir(parsed, **values)
    second = build_structural_contract_ir(parsed, **values)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_source_anchor_rejects_reversed_code_point_range() -> None:
    with pytest.raises(ValidationError):
        SourceAnchor(
            anchor_id="anchor-1",
            block_id="block-1",
            page_number=1,
            char_start=5,
            char_end=3,
        )
