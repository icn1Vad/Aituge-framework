from __future__ import annotations

from contract.ir.windowing import (
    build_section_units,
    build_section_windows,
    validate_window_coverage,
)
from contract.parser.models import ParsedContractBlock


def _block(
    block_no: int,
    text: str,
    *,
    block_type: str = "paragraph",
    metadata: dict[str, object] | None = None,
) -> ParsedContractBlock:
    return ParsedContractBlock(
        block_id=f"block-{block_no:03d}",
        block_no=block_no,
        block_type=block_type,
        text=text,
        page_number=None,
        paragraph_no=block_no,
        char_start=0,
        char_end=len(text),
        heading_path=[],
        metadata=metadata or {},
    )


def _character_count(text: str) -> int:
    return len(text)


def test_builds_hierarchy_and_records_subclauses() -> None:
    blocks = [
        _block(1, "技术服务合同"),
        _block(2, "第一章 总则", block_type="heading"),
        _block(3, "第一条 合作内容", block_type="article"),
        _block(4, "1.1 乙方提供系统建设服务。"),
        _block(5, "第二节 项目费用", block_type="heading"),
        _block(6, "第二条 合同价款", block_type="article"),
        _block(7, "合同总价为人民币十万元。"),
    ]

    sections = build_section_units(blocks)

    assert [item.section_type for item in sections] == [
        "PREAMBLE",
        "CHAPTER",
        "ARTICLE",
        "SECTION",
        "ARTICLE",
    ]
    chapter, first_article, section, second_article = sections[1:]
    assert chapter.major_section_id == chapter.section_id
    assert first_article.parent_section_id == chapter.section_id
    assert first_article.major_section_id == chapter.section_id
    assert first_article.subclause_block_ids == ("block-004",)
    assert section.parent_section_id == chapter.section_id
    assert second_article.parent_section_id == section.section_id
    assert second_article.clause_no == "第二条"


def test_packs_small_sections_without_crossing_major_chapter() -> None:
    blocks = [
        _block(1, "第一章 总则", block_type="heading"),
        _block(2, "第一条 定义", block_type="article"),
        _block(3, "本合同所称服务是指软件服务。"),
        _block(4, "第二条 合作范围", block_type="article"),
        _block(5, "乙方提供系统部署服务。"),
        _block(6, "第二章 费用", block_type="heading"),
        _block(7, "第三条 付款", block_type="article"),
        _block(8, "甲方应在验收后付款。"),
    ]

    sections = build_section_units(blocks)
    windows = build_section_windows(
        sections,
        soft_token_limit=200,
        hard_token_limit=250,
        token_estimator=_character_count,
    )

    first_chapter = sections[0].major_section_id
    second_chapter = sections[3].major_section_id
    assert first_chapter != second_chapter
    assert len(windows) == 2
    assert windows[0].primary_block_ids == tuple(f"block-{i:03d}" for i in range(1, 6))
    assert windows[1].primary_block_ids == tuple(f"block-{i:03d}" for i in range(6, 9))
    assert validate_window_coverage(blocks, windows).valid


def test_splits_oversized_block_into_exact_non_overlapping_ranges() -> None:
    text = "第一句内容较长。第二句内容也较长。第三句仍然很长。第四句结束。"
    blocks = [_block(1, text)]

    windows = build_section_windows(
        build_section_units(blocks),
        soft_token_limit=14,
        hard_token_limit=18,
        token_estimator=_character_count,
    )
    report = validate_window_coverage(blocks, windows)
    spans = [
        (offset.block_char_start, offset.block_char_end)
        for window in windows
        for offset in window.offset_map
    ]

    assert len(windows) > 1
    assert report.valid
    assert report.split_block_ids == ("block-001",)
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    assert all(window.estimated_tokens <= 18 for window in windows)


def test_offset_map_round_trips_to_original_blocks() -> None:
    blocks = [
        _block(1, "第一条 服务内容", block_type="article"),
        _block(2, "乙方应按期完成交付。"),
        _block(3, "甲方应及时组织验收。"),
    ]
    windows = build_section_windows(
        build_section_units(blocks),
        soft_token_limit=200,
        hard_token_limit=250,
        context_lines=("我方：甲方", "审查尺度：中立"),
        token_estimator=_character_count,
    )

    assert len(windows) == 1
    window = windows[0]
    assert window.context_text == "我方：甲方\n审查尺度：中立"
    for offset in window.offset_map:
        source_block = next(item for item in blocks if item.block_id == offset.block_id)
        rendered = window.source_text[offset.rendered_start : offset.rendered_end]
        original = source_block.text[offset.block_char_start : offset.block_char_end]
        assert rendered == original


def test_window_ids_and_content_are_deterministic() -> None:
    blocks = [
        _block(1, "第一条 服务内容", block_type="article"),
        _block(2, "乙方负责提供服务。"),
    ]

    first = build_section_windows(build_section_units(blocks))
    second = build_section_windows(build_section_units(blocks))

    assert first == second


def test_ignores_footer_blocks_in_sections_and_coverage() -> None:
    blocks = [
        _block(1, "第一条 服务内容", block_type="article"),
        _block(2, "乙方负责提供服务。"),
        _block(3, "第 1 页 / 共 2 页", block_type="footer"),
    ]

    sections = build_section_units(blocks)
    windows = build_section_windows(sections)
    report = validate_window_coverage(blocks, windows)

    assert report.expected_block_count == 2
    assert report.covered_block_count == 2
    assert "block-003" not in windows[0].primary_block_ids
