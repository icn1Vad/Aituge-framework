from __future__ import annotations

from contract.ir.models import IRParty, SourceAnchor
from contract.party import extract_party_candidates
from contract.parser.models import ParsedContractBlock
from services.contract.capabilities.register import _unique_party_name


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


def _party(role: str, name: str, *pages: int) -> IRParty:
    return IRParty(
        role=role,
        name=name,
        source_anchors=[
            SourceAnchor(
                anchor_id=f"anchor-{role}-{page}",
                block_id=f"block-{page}",
                page_number=page,
                char_start=0,
                char_end=len(name),
            )
            for page in pages
        ],
    )


def _party_without_page(role: str, name: str, anchor_no: int) -> IRParty:
    return IRParty(
        role=role,
        name=name,
        source_anchors=[
            SourceAnchor(
                anchor_id=f"anchor-{role}-{anchor_no}",
                block_id=f"block-{anchor_no}",
                page_number=None,
                char_start=0,
                char_end=len(name),
            )
        ],
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


def test_extracts_paired_role_labels_without_colons() -> None:
    block = _block(
        1,
        "甲方（出租方）苏杨，乙方（承租方）西安市雁塔区丈八街道办事处",
    )

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "苏杨"),
        ("PARTY_B", "西安市雁塔区丈八街道办事处"),
    ]
    for candidate in candidates:
        anchor = candidate.source_anchors[0]
        assert block.text[anchor.char_start : anchor.char_end] == candidate.name


def test_does_not_treat_unqualified_party_prose_as_paired_declaration() -> None:
    block = _block(1, "甲方应按时交付，乙方应按时付款。")

    assert extract_party_candidates([block]) == []


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


def test_strips_nested_entity_name_field_from_signature_party() -> None:
    block = _block(1, "乙方：单位名称：西安帝融商业运营管理有限公司", page_number=6)

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_B", "西安帝融商业运营管理有限公司"),
    ]
    anchor = candidates[0].source_anchors[0]
    assert block.text[anchor.char_start : anchor.char_end] == candidates[0].name


def test_reconciles_repeated_full_name_with_late_ocr_truncation() -> None:
    candidates = [
        _party("PARTY_B", "西安帝融商业运营管理有限公司", 1, 2),
        _party("PARTY_B", "西安帝融商业运营管", 6),
    ]

    assert _unique_party_name(candidates, "PARTY_B") == "西安帝融商业运营管理有限公司"


def test_keeps_distinct_complete_legal_entities_unresolved() -> None:
    candidates = [
        _party("PARTY_B", "西安帝融商业运营管理有限公司", 1, 2),
        _party("PARTY_B", "西安帝融商业运营管理有限责任公司", 6),
    ]
    assert _unique_party_name(candidates, "PARTY_B") is None


def test_reconciles_truncation_with_single_full_name_evidence() -> None:
    candidates = [
        _party_without_page("PARTY_B", "西安帝融商业运营管理有限公司", 1),
        _party_without_page("PARTY_B", "西安帝融商业运营管", 2),
    ]
    assert _unique_party_name(candidates, "PARTY_B") == "西安帝融商业运营管理有限公司"


def test_does_not_apply_enterprise_prefix_rule_to_natural_people() -> None:
    candidates = [
        _party("PARTY_A", "张三", 1),
        _party("PARTY_A", "张三丰", 2),
    ]
    assert _unique_party_name(candidates, "PARTY_A") is None
