from __future__ import annotations

import uuid

import pytest

from proof.application.chunking import split_into_clause_units
from proof.domain import DocumentBlock
from proof.errors import ProofError
from proof.infrastructure.parsers import ARTICLE_START_RE


def block(
    text: str,
    ordinal: int,
    *,
    block_type: str = "paragraph",
    page: int | None = 1,
    heading_path: list[str] | None = None,
) -> DocumentBlock:
    start = ordinal * 100
    return DocumentBlock(
        id=uuid.uuid4().hex,
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        page_no=page,
        paragraph_index=ordinal,
        heading_path=heading_path or [],
        char_start=start,
        char_end=start + len(text),
    )


@pytest.mark.parametrize(
    "value",
    ["第一条 内容", "第十二条 内容", "第一百零二条 内容", "第1条 内容", "第 12 条 内容"],
)
def test_article_number_variants(value: str) -> None:
    assert ARTICLE_START_RE.match(value)


def test_one_complete_article_is_one_unit_across_pages_and_subitems() -> None:
    blocks = [
        block("第一章 总则", 1, block_type="heading", heading_path=["第一章 总则"]),
        block("第一条 本制度适用于公司。", 2, block_type="article", heading_path=["第一章 总则"]),
        block("（一）董事会。", 3, heading_path=["第一章 总则"]),
        block("（二）审计委员会。", 4, page=2, heading_path=["第一章 总则"]),
        block("第 2 页 共 8 页", 5, block_type="footer", page=2, heading_path=["第一章 总则"]),
        block("第二章 职责", 6, block_type="heading", page=2, heading_path=["第二章 职责"]),
        block("第二条 审计部负责监督。", 7, block_type="article", page=2, heading_path=["第二章 职责"]),
    ]

    units = split_into_clause_units(blocks)

    assert len(units) == 2
    assert units[0].clause_no_raw == "第一条"
    assert units[0].text == "第一条 本制度适用于公司。\n（一）董事会。\n（二）审计委员会。"
    assert units[0].page_start == 1
    assert units[0].page_end == 2
    assert "第二章 职责" not in units[0].text
    assert units[1].heading_path == ["第二章 职责"]


def test_preamble_is_not_retrieval_content() -> None:
    units = split_into_clause_units(
        [
            block("某公司管理制度", 1),
            block("前言内容", 2),
            block("第一条 正文。", 3, block_type="article"),
        ]
    )
    assert len(units) == 1
    assert units[0].text == "第一条 正文。"


def test_long_article_is_never_split() -> None:
    long_text = "第一条 " + "完整条款内容。" * 5000
    units = split_into_clause_units([block(long_text, 1, block_type="article")])
    assert len(units) == 1
    assert units[0].text == long_text


def test_duplicate_article_numbers_are_distinct_units() -> None:
    units = split_into_clause_units(
        [
            block("第一条 第一处。", 1, block_type="article"),
            block("第一条 第二处。", 2, block_type="article"),
        ]
    )
    assert [unit.clause_ordinal for unit in units] == [1, 2]
    assert [unit.clause_no_raw for unit in units] == ["第一条", "第一条"]
    assert units[0].id != units[1].id


def test_no_clause_fails_without_fallback() -> None:
    with pytest.raises(ProofError) as exc_info:
        split_into_clause_units([block("只有普通段落，没有正式条款。", 1)])
    assert exc_info.value.code == "no_clauses_found"
