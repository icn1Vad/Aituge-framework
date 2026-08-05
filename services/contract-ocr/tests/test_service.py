from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfWriter

from contract_ocr.service import ContractOcrService, OcrSettings


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
