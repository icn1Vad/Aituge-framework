from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from pypdf import PdfWriter

import contract_ocr.service as service_module
from contract_ocr.service import (
    ContractOcrService,
    OcrSettings,
    RenderedTile,
)


class FakeEngine:
    initialized = True

    def parse_page(self, _: Path):
        return [
            {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {
                        "block_id": "1",
                        "block_order": 1,
                        "block_label": "text",
                        "block_content": "甲方：测试甲公司\n乙方：测试乙公司",
                        "block_bbox": [1, 2, 90, 30],
                    },
                    {
                        "block_id": "2",
                        "block_order": 2,
                        "block_label": "table",
                        "block_content": "<table><tr><td>金额</td><td>100</td></tr></table>",
                        "block_bbox": [1, 40, 90, 80],
                    },
                ],
                "table_res_list": [{"cell_box_list": [[1, 40, 45, 80], [45, 40, 90, 80]]}],
            }
        ]


def scanned_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class TwoPageEngine:
    initialized = True

    def parse_page(self, _: Path):
        return [
            {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {
                        "block_id": "1",
                        "block_order": 1,
                        "block_label": "section_title",
                        "block_content": "Section nine",
                    }
                ],
            },
            {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {
                        "block_id": "2",
                        "block_order": 1,
                        "block_label": "text",
                        "block_content": "The parties shall negotiate to resolve disputes.",
                    }
                ],
            },
        ]


def test_scanned_pdf_becomes_docx_and_manifest() -> None:
    service = ContractOcrService(OcrSettings(), engine=FakeEngine())
    result = service.convert_pdf("scan.pdf", scanned_pdf())
    assert result.inspection.classification == "OCR_REQUIRED"
    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        assert set(archive.namelist()) == {"contract.docx", "manifest.json"}
        assert archive.read("contract.docx").startswith(b"PK")
        assert b"cell_box_list" in archive.read("manifest.json")


def test_docx_does_not_force_source_pdf_page_breaks() -> None:
    service = ContractOcrService(OcrSettings(), engine=TwoPageEngine())
    result = service.convert_pdf("scan.pdf", scanned_pdf())
    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        contract_docx = archive.read("contract.docx")
    with zipfile.ZipFile(io.BytesIO(contract_docx)) as document_archive:
        document_xml = ElementTree.fromstring(document_archive.read("word/document.xml"))

    word_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    namespaces = {"w": word_namespace}
    assert not document_xml.findall(".//w:br[@w:type='page']", namespaces)

    paragraph_text = [
        "".join(node.itertext())
        for node in document_xml.findall(".//w:body/w:p", namespaces)
    ]
    assert paragraph_text == ["Section nine", "The parties shall negotiate to resolve disputes."]


class XmlControlCharacterEngine:
    initialized = True

    def parse_page(self, _: Path):
        return [
            {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {
                        "block_id": "1",
                        "block_order": 1,
                        "block_label": "text",
                        "block_content": "甲方：测试甲公司\x00\x0b\n乙方：测试乙公司\uffff",
                    },
                    {
                        "block_id": "2",
                        "block_order": 2,
                        "block_label": "table",
                        "block_content": "<table><tr><td>金额\x00</td><td>100\x0b</td></tr></table>",
                    },
                ],
            }
        ]


def test_docx_strips_characters_forbidden_by_xml() -> None:
    service = ContractOcrService(OcrSettings(), engine=XmlControlCharacterEngine())

    result = service.convert_pdf("scan.pdf", scanned_pdf())

    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        contract_docx = archive.read("contract.docx")
    with zipfile.ZipFile(io.BytesIO(contract_docx)) as document_archive:
        document_xml = document_archive.read("word/document.xml")
    assert "测试甲公司".encode("utf-8") in document_xml
    assert "测试乙公司".encode("utf-8") in document_xml
    assert "金额".encode("utf-8") in document_xml


class NativeTextEngine:
    initialized = False

    def parse_page(self, _: Path):
        raise AssertionError("native text PDF must not initialize or invoke PaddleOCR")


class NativeTextPage:
    mediabox = SimpleNamespace(width=595, height=842)

    def extract_text(self) -> str:
        return """甲方：测试甲公司
乙方：测试乙公司
合同金额：100元"""


def test_native_text_pdf_becomes_docx_without_paddle(monkeypatch) -> None:
    reader = SimpleNamespace(pages=[NativeTextPage()])
    monkeypatch.setattr(service_module, "_open_pdf", lambda _: reader)
    service = ContractOcrService(OcrSettings(min_text_chars_per_page=4), engine=NativeTextEngine())

    result = service.convert_pdf("native.pdf", b"%PDF-native-test")

    assert result.inspection.classification == "NATIVE_TEXT"
    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        contract_docx = archive.read("contract.docx")
    assert manifest["engine"]["name"] == "PDF_TEXT_LAYER"
    assert manifest["pages_requiring_ocr"] == []
    with zipfile.ZipFile(io.BytesIO(contract_docx)) as document_archive:
        document_xml = document_archive.read("word/document.xml")
    assert "测试甲公司".encode("utf-8") in document_xml
    assert "测试乙公司".encode("utf-8") in document_xml


class OffsetRenderer:
    def render_tiles(self, _: Path, workdir: Path) -> list[RenderedTile]:
        path = workdir / "tile.png"
        path.write_bytes(b"fake")
        return [
            RenderedTile(
                page_number=1,
                page_count=1,
                tile_index=1,
                y_offset=100,
                full_width=100,
                full_height=300,
                path=path,
            )
        ]


def test_structure_pdf_offsets_tile_coordinates() -> None:
    service = ContractOcrService(
        OcrSettings(),
        engine=FakeEngine(),
        renderer=OffsetRenderer(),
    )

    result = service.structure_pdf(scanned_pdf())

    assert result.inspection.classification == "OCR_REQUIRED"
    assert result.pages[0]["height"] == 300
    assert result.pages[0]["blocks"][0]["bbox"] == [1, 102, 90, 130]
    assert result.pages[0]["blocks"][0]["tile_index"] == 1
    assert result.pages[0]["tables"][0]["cell_box_list"] == [
        [1, 140, 45, 180],
        [45, 140, 90, 180],
    ]
