from lxml import etree

from translation_service.processors.docx import (
    NS,
    _apply_mapped_translation,
    _apply_style_degradation,
    _build_unit,
    _logical_paragraph_groups,
)


def _document(xml_body: str):
    return etree.fromstring(
        f"""
        <w:document xmlns:w="{NS['w']}">
          <w:body>{xml_body}</w:body>
        </w:document>
        """.encode()
    )


def test_paragraph_is_one_logical_unit_with_run_markers() -> None:
    root = _document(
        """
        <w:p>
          <w:r><w:rPr><w:b/></w:rPr><w:t>Hello </w:t></w:r>
          <w:r><w:t>world</w:t></w:r>
        </w:p>
        """
    )
    paragraphs = _logical_paragraph_groups(root)
    assert len(paragraphs) == 1
    unit = _build_unit("word/document.xml", 1, paragraphs[0])
    assert unit is not None
    assert unit.plain_text == "Hello world"
    assert unit.marked_text == "⟦R0001⟧Hello ⟦/R0001⟧⟦R0002⟧world⟦/R0002⟧"

    assert _apply_mapped_translation(
        unit, "⟦R0001⟧你好，⟦/R0001⟧⟦R0002⟧世界⟦/R0002⟧"
    )
    texts = root.xpath(".//w:t/text()", namespaces=NS)
    assert texts == ["你好，", "世界"]


def test_table_cell_is_one_unit_across_paragraphs() -> None:
    root = _document(
        """
        <w:tbl><w:tr><w:tc>
          <w:p><w:r><w:t>First</w:t></w:r></w:p>
          <w:p><w:r><w:t>Second</w:t></w:r></w:p>
        </w:tc></w:tr></w:tbl>
        """
    )
    groups = _logical_paragraph_groups(root)
    assert len(groups) == 1
    assert len(groups[0]) == 2
    unit = _build_unit("word/document.xml", 1, groups[0])
    assert unit is not None
    assert unit.plain_text == "First\nSecond"
    assert "⟦P0001⟧" in unit.marked_text


def test_explicit_style_degradation_uses_first_run() -> None:
    root = _document(
        """
        <w:p>
          <w:r><w:rPr><w:b/></w:rPr><w:t>Hello</w:t></w:r>
          <w:r><w:rPr><w:i/></w:rPr><w:t>world</w:t></w:r>
        </w:p>
        """
    )
    unit = _build_unit("word/document.xml", 1, _logical_paragraph_groups(root)[0])
    assert unit is not None

    _apply_style_degradation(unit, "完整译文")

    text_nodes = root.xpath(".//w:t", namespaces=NS)
    assert text_nodes[0].text == "完整译文"
    assert text_nodes[1].text == ""
    assert root.xpath("count(.//w:r[1]/w:rPr/w:b)", namespaces=NS) == 1
