from __future__ import annotations

from contract.party import extract_party_candidates
from contract.parser.models import ParsedContractBlock


def _block(block_no: int, text: str, *, page_number: int | None = 1) -> ParsedContractBlock:
    return ParsedContractBlock(
        block_id=f"block-{block_no}",
        block_no=block_no,
        block_type="paragraph",
        text=text,
        page_number=page_number,
        paragraph_no=block_no,
        char_start=0,
        char_end=len(text),
        heading_path=[],
        metadata={},
    )


def test_extracts_chinese_aliases_and_exact_source_anchors() -> None:
    block = _block(1, "委托方（甲方）：某某科技有限公司 受托方（乙方）：某某服务中心")

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "某某科技有限公司"),
        ("PARTY_B", "某某服务中心"),
    ]
    for candidate in candidates:
        anchor = candidate.source_anchors[0]
        assert anchor.block_id == block.block_id
        assert anchor.page_number == 1
        assert block.text[anchor.char_start : anchor.char_end] == candidate.name


def test_extracts_english_party_labels_from_one_block() -> None:
    block = _block(1, "Party A: Acme Holdings Ltd.; Party B: Beta Services LLC")

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "Acme Holdings Ltd."),
        ("PARTY_B", "Beta Services LLC"),
    ]


def test_deduplicates_repeated_signature_names_and_keeps_all_anchors() -> None:
    candidates = extract_party_candidates(
        [
            _block(1, "甲方：某某有限公司"),
            _block(2, "甲方（盖章）：某某有限公司", page_number=3),
            _block(3, "乙方（盖章）："),
            _block(4, "乙方：（签字或盖章）"),
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].role == "PARTY_A"
    assert candidates[0].name == "某某有限公司"
    assert [anchor.page_number for anchor in candidates[0].source_anchors] == [1, 3]
