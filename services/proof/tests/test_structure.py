from __future__ import annotations

import uuid

import pytest

from proof.application.structure import extract_policy_structure
from proof.application.structure_validation import validate_structure_extraction
from proof.domain import DocumentBlock
from proof.domain.numbering import MarkerKind, detect_numbering_marker
from proof.errors import ProofError


def test_numbering_recognition_is_normalized_without_changing_source_text() -> None:
    marker = detect_numbering_marker("  ５．２．１ 供应商准入")
    assert marker is not None
    assert marker.kind == MarkerKind.DECIMAL
    assert marker.normalized == "5.2.1"
    assert marker.depth == 3


def test_decimal_numbering_accepts_terminal_period() -> None:
    marker = detect_numbering_marker("6.2. 费用管理和报销基本规定")
    assert marker is not None
    assert marker.kind == MarkerKind.DECIMAL
    assert marker.raw == "6.2"
    assert marker.normalized == "6.2"
    assert marker.path == (6, 2)


def test_article_owns_nested_numbered_lists() -> None:
    result = extract_policy_structure(
        [
            block("第一条 适用范围。", 1),
            block("一、适用于总部。", 2),
            block("（一）财务部门。", 3),
            block("1. 具体要求。", 4),
            block("第二条 职责。", 5),
        ]
    )
    assert result.profile == "article"
    assert len(result.units) == 2
    assert result.units[0].text == "第一条 适用范围。\n一、适用于总部。\n（一）财务部门。\n1. 具体要求。"


def test_decimal_outline_uses_second_level_and_keeps_deeper_nodes() -> None:
    result = extract_policy_structure(
        [
            block("制度名称", 1),
            block("1 目的", 2),
            block("本制度用于规范管理。", 3),
            block("2 职责", 4),
            block("2.1 采购部门", 5),
            block("2.1.1 负责准入。", 6),
            block("2.2 财务部门", 7),
            block("2.2.1 负责付款。", 8),
            block("3 附则", 9),
            block("本制度自发布之日起施行。", 10),
        ]
    )
    assert result.profile == "decimal_outline"
    assert [unit.clause_no_raw for unit in result.units] == ["1", "2.1", "2.2", "3"]
    assert result.units[1].text == "2.1 采购部门\n2.1.1 负责准入。"
    assert result.units[1].heading_path == ["2 职责"]


def test_decimal_excerpt_without_root_ignores_flat_numbered_lists() -> None:
    result = extract_policy_structure(
        [
            block("6.2. 费用管理和报销基本规定", 1),
            block("6.2.1. 报销人对票据真实性负责。", 2),
            block("6.2.2. 费用应及时报销。", 3),
            block("6.3. 费用报销原始单据规定", 4),
            block("6.3.1. 电子发票应可以查验。", 5),
            block("6.4. 差旅费", 6),
            block("6.4.1. 出差管理", 7),
            block("1. 调剂地区不超过限额。", 8),
            block("2. 多人出差可合住。", 9),
            block("3. 会议统一安排食宿时不补助。", 10),
            block("限额为 2.5 万元。", 11),
            block("6.4.2. 差旅费支出范围", 12),
        ]
    )
    assert result.profile == "decimal_outline"
    assert [unit.clause_no_raw for unit in result.units] == [
        "6.2.1",
        "6.2.2",
        "6.3.1",
        "6.4.1",
        "6.4.2",
    ]
    assert result.units[0].text.startswith("6.2. 费用管理")
    assert result.units[0].heading_path == ["6.2. 费用管理和报销基本规定"]
    assert "1. 调剂地区不超过限额。" in result.units[3].text
    assert "3. 会议统一安排食宿时不补助。" in result.units[3].text
    assert "限额为 2.5 万元。" in result.units[3].text
    assert result.diagnostics["content_coverage_rate"] == 1.0


def test_chinese_outline_uses_parenthesized_second_level() -> None:
    result = extract_policy_structure(
        [
            block("一、适用范围", 1),
            block("（一）总部", 2),
            block("1. 包括职能部门。", 3),
            block("（二）下属单位", 4),
            block("二、职责", 5),
            block("由办公室负责解释。", 6),
        ]
    )
    assert result.profile == "chinese_outline"
    assert [unit.clause_no_raw for unit in result.units] == ["（一）", "（二）", "二、"]
    assert "1. 包括职能部门。" in result.units[0].text


def test_flat_arabic_outline_without_space_is_supported() -> None:
    result = extract_policy_structure(
        [
            block("1.目的", 1),
            block("规范供应商管理。", 2),
            block("2.职责", 3),
            block("采购部门负责准入。", 4),
            block("3.附则", 5),
            block("自发布之日起施行。", 6),
        ]
    )
    assert result.profile == "decimal_outline"
    assert [unit.clause_no_raw for unit in result.units] == ["1", "2", "3"]


def test_separate_decimal_and_article_regions_are_mixed() -> None:
    result = extract_policy_structure(
        [
            block("1 目的", 1),
            block("正文。", 2),
            block("2 职责", 3),
            block("2.1 采购部门", 4),
            block("2.2 财务部门", 5),
            block("3 附则", 6),
            block("附录A 业务细则", 7, block_type="heading"),
            block("第一条 附录规则。", 8),
            block("第二条 另一规则。", 9),
        ]
    )
    assert result.profile == "mixed"
    assert result.profiles_detected == ["decimal_outline", "article"]
    assert [unit.unit_type for unit in result.units] == [
        "decimal_outline",
        "decimal_outline",
        "decimal_outline",
        "decimal_outline",
        "article",
        "article",
    ]
    assert any(item["code"] == "mixed_numbering_profiles" for item in result.warnings)


def test_article_preamble_revision_list_is_not_a_separate_region() -> None:
    result = extract_policy_structure(
        [
            block("修订说明", 1),
            block("1、 增加供应商评估要求。", 2),
            block("2、 删除旧表单。", 3),
            block("第一章 总则", 4, block_type="heading"),
            block("第一条 正文。", 5),
        ]
    )
    assert result.profile == "article"
    assert len(result.units) == 1
    assert result.units[0].text == "第一条 正文。"


def test_no_stable_numbering_sequence_still_fails() -> None:
    with pytest.raises(ProofError) as exc_info:
        extract_policy_structure([block("前言", 1), block("普通正文。", 2)])
    assert exc_info.value.code == "no_clauses_found"


def test_structure_validation_accepts_valid_boundaries_and_hashes() -> None:
    blocks = [
        block("第一条 范围。", 1),
        block("（一）内部子项。", 2),
        block("第二条 职责。", 3),
    ]
    extraction = extract_policy_structure(blocks)
    assert validate_structure_extraction(blocks, extraction) == []


def test_structure_validation_rejects_overlapping_source_blocks() -> None:
    blocks = [block("第一条 范围。", 1), block("第二条 职责。", 2)]
    extraction = extract_policy_structure(blocks)
    extraction.units[1].source_block_ids.append(extraction.units[0].source_block_ids[0])
    codes = {item["code"] for item in validate_structure_extraction(blocks, extraction)}
    assert "overlapping_source_blocks" in codes
    assert "source_block_order_invalid" in codes


def block(text: str, ordinal: int, *, block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        id=uuid.uuid4().hex,
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        paragraph_index=ordinal,
        char_start=ordinal * 100,
        char_end=ordinal * 100 + len(text),
    )
