from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable

from contract.errors import ContractError
from contract.parser.models import ParsedContract, ParsedContractBlock


PARSER_VERSION = "contract-parser-v1.2"
SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx"})
_OLE_COMPOUND_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_CHINESE_DIGITS = "〇零一二三四五六七八九十百千万两"
_NUMBER_TOKEN = rf"[{_CHINESE_DIGITS}0-9０-９]+"
_ARTICLE_RE = re.compile(rf"^[\s\u3000]*第[\s\u3000]*{_NUMBER_TOKEN}[\s\u3000]*条")
_CHAPTER_RE = re.compile(
    rf"^[\s\u3000]*(第[\s\u3000]*{_NUMBER_TOKEN}[\s\u3000]*(?:编|章|篇))[\s\u3000]*(.*)$"
)
_SECTION_RE = re.compile(rf"^[\s\u3000]*(第[\s\u3000]*{_NUMBER_TOKEN}[\s\u3000]*节)[\s\u3000]*(.*)$")
_PAGE_FOOTER_RE = re.compile(r"^第?\s*\d+\s*页(?:\s*共\s*\d+\s*页)?$")
_LITERAL_NUMBER_MARKER_RE = re.compile(
    r"^(?P<marker>\s*(?:\d+(?:[.．]\d+){0,6}[、.．]?|"
    r"[（(][一二三四五六七八九十百千万〇零0-9A-Za-z]+[）)]|"
    r"[一二三四五六七八九十百千万〇零]+、))\s*"
)
_MAX_DOCX_MEMBER_COUNT = 10_000
_MAX_DOCX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


class NativeContractParser:
    version = PARSER_VERSION
    supported_extensions = SUPPORTED_EXTENSIONS

    def parse(self, path: Path, *, generation_id: str) -> ParsedContract:
        extension = path.suffix.lower()
        if extension not in self.supported_extensions:
            raise ContractError(
                "FILE_TYPE_UNSUPPORTED",
                "第一阶段仅支持具有文本层的PDF和DOCX合同",
                status_code=415,
                user_action_required=True,
            )
        if extension == ".pdf":
            return self._parse_pdf(path, generation_id=generation_id)
        return self._parse_docx(path, generation_id=generation_id)

    @staticmethod
    def _parse_pdf(path: Path, *, generation_id: str) -> ParsedContract:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
        except Exception as exc:
            raise _corrupted("PDF文件无法解析", exc) from exc
        if reader.is_encrypted:
            raise ContractError(
                "FILE_ENCRYPTED",
                "第一阶段不支持加密PDF合同",
                status_code=422,
                user_action_required=True,
            )
        if not reader.pages:
            raise ContractError(
                "FILE_CORRUPTED",
                "PDF文件不包含有效页面",
                status_code=422,
                user_action_required=True,
            )

        builder = _BlockBuilder(generation_id)
        pages_without_text: list[int] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                raise _corrupted(f"PDF第{page_number}页无法解析", exc) from exc
            if not page_text.strip():
                pages_without_text.append(page_number)
                continue
            for line in page_text.splitlines():
                builder.add(line, page_number=page_number)
        if pages_without_text and builder.blocks:
            raise ContractError(
                "OCR_PREPROCESS_REQUIRED",
                "PDF包含无法读取文字的页面，需先完成OCR预处理",
                status_code=422,
                user_action_required=True,
            )
        if not builder.blocks:
            raise ContractError(
                "SCANNED_DOCUMENT_UNSUPPORTED",
                "PDF没有可选择文本，第一阶段不支持扫描件合同",
                status_code=422,
                user_action_required=True,
            )
        return ParsedContract(
            file_type="pdf",
            blocks=builder.blocks,
            page_count=len(reader.pages),
            warnings=[],
        )

    @staticmethod
    def _parse_docx(path: Path, *, generation_id: str) -> ParsedContract:
        _validate_docx_package(path)
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph

            document = Document(str(path))
        except Exception as exc:
            raise _corrupted("DOCX文件无法解析", exc) from exc

        builder = _BlockBuilder(generation_id)
        table_number = 0
        for item in _iter_docx_body(document):
            if isinstance(item, Paragraph):
                style_name = str(getattr(item.style, "name", "") or "")
                preferred = (
                    "heading"
                    if style_name.lower().startswith("heading") or style_name.startswith("标题")
                    else None
                )
                metadata = _docx_paragraph_metadata(item, style_name)
                for line in item.text.splitlines():
                    builder.add(line, block_type=preferred, metadata=metadata)
            elif isinstance(item, Table):
                table_number += 1
                for row_number, row in enumerate(item.rows, start=1):
                    values = [_normalize_table_cell(cell.text) for cell in row.cells]
                    if any(values):
                        builder.add(
                            " | ".join(values),
                            block_type="table_row",
                            metadata={"table_no": table_number, "row_no": row_number},
                        )
        if not builder.blocks:
            raise ContractError(
                "CONTRACT_PARSE_FAILED",
                "DOCX合同不包含可审查文本",
                status_code=422,
                user_action_required=True,
            )
        return ParsedContract(file_type="docx", blocks=builder.blocks, page_count=None)


class _BlockBuilder:
    def __init__(self, generation_id: str) -> None:
        self.generation_id = generation_id
        self.blocks: list[ParsedContractBlock] = []
        self.cursor = 0
        self.paragraph_no = 0
        self.chapter = ""
        self.section = ""
        self.outline_headings: list[str] = []

    def add(
        self,
        text: str,
        *,
        page_number: int | None = None,
        block_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        raw = str(text or "")
        value = raw.strip()
        if not value:
            return
        actual_type, heading = _classify_block(value, block_type)
        if actual_type == "heading":
            if _CHAPTER_RE.match(value):
                self.chapter = heading
                self.section = ""
                self.outline_headings = []
            elif _SECTION_RE.match(value):
                self.section = heading
                self.outline_headings = []
            elif block_type == "heading":
                outline_level = _outline_level(metadata)
                self.outline_headings = self.outline_headings[:outline_level]
                self.outline_headings.append(value)
        self.paragraph_no += 1
        block_no = len(self.blocks) + 1
        start = self.cursor
        end = start + len(value)
        block_metadata = dict(metadata or {})
        block_metadata.setdefault("leading_whitespace", len(raw) - len(raw.lstrip()))
        identity = "\0".join(
            (
                self.generation_id,
                str(block_no),
                actual_type,
                str(page_number or ""),
                value,
            )
        )
        block_id = "block-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        self.blocks.append(
            ParsedContractBlock(
                block_id=block_id,
                block_no=block_no,
                block_type=actual_type,
                text=value,
                page_number=page_number,
                paragraph_no=self.paragraph_no,
                char_start=start,
                char_end=end,
                heading_path=[
                    item
                    for item in (
                        *self.outline_headings,
                        self.chapter,
                        self.section,
                    )
                    if item
                ],
                metadata=block_metadata,
            )
        )
        self.cursor = end + 1


def _classify_block(text: str, preferred: str | None) -> tuple[str, str]:
    if _PAGE_FOOTER_RE.match(text.replace("\u3000", " ").strip()):
        return "footer", ""
    chapter = _CHAPTER_RE.match(text)
    if chapter:
        return "heading", " ".join(item for item in chapter.groups() if item).strip()
    section = _SECTION_RE.match(text)
    if section:
        return "heading", " ".join(item for item in section.groups() if item).strip()
    if preferred == "heading":
        return "heading", text
    if _ARTICLE_RE.match(text):
        return "article", ""
    return preferred or "paragraph", ""


def _validate_docx_package(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(8)
    except OSError as exc:
        raise _corrupted("DOCX文件无法读取", exc) from exc
    if prefix == _OLE_COMPOUND_SIGNATURE:
        raise ContractError(
            "FILE_ENCRYPTED",
            "第一阶段不支持加密DOCX合同",
            status_code=422,
            user_action_required=True,
        )
    try:
        with zipfile.ZipFile(path) as package:
            members = package.infolist()
            uncompressed_size = sum(item.file_size for item in members)
            if (
                len(members) > _MAX_DOCX_MEMBER_COUNT
                or uncompressed_size > _MAX_DOCX_UNCOMPRESSED_BYTES
            ):
                raise ContractError(
                    "FILE_CORRUPTED",
                    "DOCX文件展开后超过安全限制",
                    status_code=422,
                    user_action_required=True,
                )
            names = {member.filename for member in members}
            if any(member.flag_bits & 0x1 for member in members):
                raise ContractError(
                    "FILE_ENCRYPTED",
                    "第一阶段不支持加密DOCX合同",
                    status_code=422,
                    user_action_required=True,
                )
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ContractError(
                    "FILE_CORRUPTED",
                    "DOCX文件结构无效",
                    status_code=422,
                    user_action_required=True,
                )
            bad_member = package.testzip()
            if bad_member is not None:
                raise ContractError(
                    "FILE_CORRUPTED",
                    "DOCX文件校验失败",
                    status_code=422,
                    user_action_required=True,
                )
    except ContractError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise _corrupted("DOCX文件无法解析", exc) from exc


def _iter_docx_body(document) -> Iterable[object]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def _docx_paragraph_metadata(paragraph, style_name: str) -> dict[str, Any]:
    paragraph_format = paragraph.paragraph_format
    left_indent = paragraph_format.left_indent
    first_line_indent = paragraph_format.first_line_indent
    outline_level = None
    properties = getattr(paragraph._p, "pPr", None)
    if properties is not None and properties.outlineLvl is not None:
        outline_level = int(properties.outlineLvl.val)
    metadata: dict[str, Any] = {
        "style": style_name,
        "left_indent_twips": int(left_indent.twips) if left_indent is not None else None,
        "first_line_indent_twips": int(first_line_indent.twips) if first_line_indent is not None else None,
        "outline_level": outline_level,
        "container_path": "document/body",
    }
    native_numbering = _docx_effective_numbering(paragraph)
    if native_numbering is not None:
        metadata["native_numbering"] = native_numbering
    literal_marker = _LITERAL_NUMBER_MARKER_RE.match(str(paragraph.text or ""))
    if literal_marker is not None:
        metadata["literal_marker"] = literal_marker.group("marker").strip()
    return metadata


def _docx_effective_numbering(paragraph) -> dict[str, int | str] | None:
    """Return the effective native Word list relationship when present.

    Native list properties can be direct paragraph formatting or inherited
    from a paragraph style/base style. We deliberately preserve the effective
    numId + level rather than turning the displayed number into text.
    """

    try:
        from docx.oxml.ns import qn
    except Exception:  # pragma: no cover - python-docx is already required above
        return None

    def from_properties(properties, source: str) -> dict[str, int | str] | None:
        if properties is None:
            return None
        num_pr = properties.find(qn("w:numPr"))
        if num_pr is None:
            return None
        num_id = num_pr.find(qn("w:numId"))
        level = num_pr.find(qn("w:ilvl"))
        num_id_value = num_id.get(qn("w:val")) if num_id is not None else None
        level_value = level.get(qn("w:val")) if level is not None else "0"
        try:
            if num_id_value is None:
                return None
            return {
                "mode": "NATIVE",
                "effective_num_id": int(num_id_value),
                "list_level": int(level_value or "0"),
                "source": source,
            }
        except (TypeError, ValueError):
            return None

    direct = from_properties(getattr(paragraph._p, "pPr", None), "direct")
    if direct is not None:
        return direct
    style = getattr(paragraph, "style", None)
    seen: set[str] = set()
    while style is not None:
        style_id = str(getattr(style, "style_id", "") or id(style))
        if style_id in seen:
            break
        seen.add(style_id)
        inherited = from_properties(getattr(getattr(style, "_element", None), "pPr", None), "style")
        if inherited is not None:
            return inherited
        style = getattr(style, "base_style", None)
    return None


def _normalize_table_cell(value: str) -> str:
    return " ".join(item.strip() for item in value.splitlines() if item.strip())


def _outline_level(metadata: dict[str, Any] | None) -> int:
    if metadata is None:
        return 0
    value = metadata.get("outline_level")
    return value if isinstance(value, int) and value >= 0 else 0


def _corrupted(message: str, _exc: Exception) -> ContractError:
    return ContractError(
        "FILE_CORRUPTED",
        message,
        status_code=422,
        user_action_required=True,
    )
