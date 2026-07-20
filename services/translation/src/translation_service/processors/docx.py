from __future__ import annotations

import re
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from translation_service.domain.models import GlossaryEntry, TranslationLanguage
from translation_service.errors import TranslationError
from translation_service.model.translator import (
    BaseModelTranslator,
    MarkedTranslationUnit,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}
W_T = f"{{{W_NS}}}t"
W_TC = f"{{{W_NS}}}tc"

_TRANSLATABLE_PART = re.compile(
    r"^word/(document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml$"
)
_TOKEN_FULL = re.compile(r"⟦(?:/?R|P|T|B|O|F)\d{4}⟧")

NoticeHandler = Callable[[str, str, str], Awaitable[None]]


@dataclass(slots=True)
class RunSpan:
    marker_id: str
    text_nodes: list[etree._Element]

    @property
    def start_marker(self) -> str:
        return f"⟦{self.marker_id}⟧"

    @property
    def end_marker(self) -> str:
        return f"⟦/{self.marker_id}⟧"


@dataclass(slots=True)
class LogicalUnit:
    unit_id: str
    part_name: str
    spans: list[RunSpan]
    marked_text: str
    plain_text: str
    expected_tokens: list[str]


@dataclass(frozen=True, slots=True)
class DocxTranslationResult:
    output_path: Path
    source_character_count: int
    result_character_count: int
    unit_count: int
    style_degradation_count: int


class DocxTranslator:
    def __init__(self, model: BaseModelTranslator) -> None:
        self._model = model

    def extract_plain_text(self, path: Path, *, max_chars: int | None = None) -> str:
        roots = self._load_xml_parts(path)
        texts: list[str] = []
        used = 0
        unit_index = 0
        for part_name, root in roots.items():
            for paragraphs in _logical_paragraph_groups(root):
                unit_index += 1
                unit = _build_unit(part_name, unit_index, paragraphs)
                if unit is None:
                    continue
                text = unit.plain_text.strip()
                if not text:
                    continue
                if max_chars is not None:
                    remaining = max_chars - used
                    if remaining <= 0:
                        return "\n".join(texts)
                    text = text[:remaining]
                texts.append(text)
                used += len(text)
        return "\n".join(texts)

    async def translate(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_language: TranslationLanguage,
        target_language: TranslationLanguage,
        tenant_id: str,
        glossary: list[GlossaryEntry],
        on_notice: NoticeHandler,
    ) -> DocxTranslationResult:
        roots = self._load_xml_parts(source_path)
        for part_name in self._find_skipped_text_parts(source_path, set(roots)):
            await on_notice(
                "DOCX_PART_SKIPPED",
                part_name,
                "OpenXML text part is outside the supported translation scope",
            )
        units: list[LogicalUnit] = []
        unit_index = 0
        for part_name, root in roots.items():
            for paragraphs in _logical_paragraph_groups(root):
                unit_index += 1
                unit = _build_unit(part_name, unit_index, paragraphs)
                if unit is not None:
                    units.append(unit)
        if not units:
            raise TranslationError(
                "NO_TRANSLATABLE_TEXT", "DOCX does not contain translatable text"
            )

        marked = [
            MarkedTranslationUnit(unit_id=unit.unit_id, marked_text=unit.marked_text)
            for unit in units
        ]
        translations = await self._model.translate_marked_units(
            units=marked,
            source=source_language,
            target=target_language,
            tenant_id=tenant_id,
            glossary=glossary,
        )

        result_chars = 0
        degradation_count = 0
        for unit in units:
            translated = translations[unit.unit_id]
            if _apply_mapped_translation(unit, translated):
                result_chars += len(_strip_markers(translated))
                continue
            degradation_count += 1
            degraded = await self._model.translate_text(
                text=unit.plain_text,
                source=source_language,
                target=target_language,
                tenant_id=tenant_id,
                glossary=glossary,
            )
            _apply_style_degradation(unit, degraded)
            result_chars += len(degraded)
            await on_notice(
                "DOCX_STYLE_DEGRADED",
                unit.unit_id,
                "Run marker mapping failed; translation was placed in the first text run",
            )

        self._write_translated_archive(source_path, output_path, roots)
        return DocxTranslationResult(
            output_path=output_path,
            source_character_count=sum(len(unit.plain_text) for unit in units),
            result_character_count=result_chars,
            unit_count=len(units),
            style_degradation_count=degradation_count,
        )

    @staticmethod
    def _load_xml_parts(path: Path) -> dict[str, etree._Element]:
        roots: dict[str, etree._Element] = {}
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            recover=False,
            huge_tree=False,
        )
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if not _TRANSLATABLE_PART.fullmatch(info.filename):
                        continue
                    roots[info.filename] = etree.fromstring(
                        archive.read(info), parser=parser
                    )
        except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
            raise TranslationError(
                "INVALID_DOCX_XML", "DOCX OpenXML content could not be parsed"
            ) from exc
        if "word/document.xml" not in roots:
            raise TranslationError("INVALID_DOCX", "DOCX main document part is missing")
        return roots

    @staticmethod
    def _find_skipped_text_parts(
        path: Path, translated_parts: set[str]
    ) -> list[str]:
        skipped: list[str] = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if (
                    info.filename.startswith("word/")
                    and info.filename.endswith(".xml")
                    and info.filename not in translated_parts
                    and b"<w:t" in archive.read(info)
                ):
                    skipped.append(info.filename)
        return skipped

    @staticmethod
    def _write_translated_archive(
        source_path: Path,
        output_path: Path,
        roots: dict[str, etree._Element],
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(
                output_path, "x"
            ) as destination:
                for info in source.infolist():
                    if info.filename in roots:
                        payload = etree.tostring(
                            roots[info.filename],
                            encoding="UTF-8",
                            xml_declaration=True,
                        )
                    else:
                        payload = source.read(info)
                    destination.writestr(info, payload)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise


def _logical_paragraph_groups(
    root: etree._Element,
) -> list[list[etree._Element]]:
    groups: list[list[etree._Element]] = []
    handled_cells: set[str] = set()
    tree = root.getroottree()
    for paragraph in root.xpath(".//w:p", namespaces=NS):
        cell = _nearest_ancestor(paragraph, W_TC)
        if cell is None:
            groups.append([paragraph])
            continue
        cell_identity = tree.getpath(cell)
        if cell_identity in handled_cells:
            continue
        handled_cells.add(cell_identity)
        cell_paragraphs = [
            candidate
            for candidate in cell.xpath(".//w:p", namespaces=NS)
            if _nearest_ancestor(candidate, W_TC) == cell
        ]
        if cell_paragraphs:
            groups.append(cell_paragraphs)
    return groups


def _nearest_ancestor(
    element: etree._Element, qualified_tag: str
) -> etree._Element | None:
    parent = element.getparent()
    while parent is not None:
        if parent.tag == qualified_tag:
            return parent
        parent = parent.getparent()
    return None


def _build_unit(
    part_name: str,
    unit_index: int,
    paragraphs: list[etree._Element],
) -> LogicalUnit | None:
    marked_parts: list[str] = []
    plain_parts: list[str] = []
    expected_tokens: list[str] = []
    spans: list[RunSpan] = []
    run_marker_index = 0
    protected_marker_index = 0

    def append_token(prefix: str) -> None:
        nonlocal protected_marker_index
        protected_marker_index += 1
        token = f"⟦{prefix}{protected_marker_index:04d}⟧"
        marked_parts.append(token)
        expected_tokens.append(token)

    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        if paragraph_index > 1:
            token = f"⟦P{paragraph_index - 1:04d}⟧"
            marked_parts.append(token)
            expected_tokens.append(token)
            plain_parts.append("\n")
        for run in paragraph.xpath(".//w:r", namespaces=NS):
            pending_text_nodes: list[etree._Element] = []

            def flush_text_nodes() -> None:
                nonlocal run_marker_index
                if not pending_text_nodes:
                    return
                source_text = "".join(node.text or "" for node in pending_text_nodes)
                if source_text:
                    run_marker_index += 1
                    marker_id = f"R{run_marker_index:04d}"
                    span = RunSpan(marker_id, list(pending_text_nodes))
                    spans.append(span)
                    marked_parts.extend(
                        (span.start_marker, source_text, span.end_marker)
                    )
                    expected_tokens.extend((span.start_marker, span.end_marker))
                    plain_parts.append(source_text)
                pending_text_nodes.clear()

            for child in run:
                local_name = etree.QName(child).localname
                if child.tag == W_T:
                    pending_text_nodes.append(child)
                    continue
                flush_text_nodes()
                if local_name == "tab":
                    append_token("T")
                    plain_parts.append("\t")
                elif local_name == "br":
                    append_token("B")
                    plain_parts.append("\n")
                elif local_name in {"drawing", "object", "pict", "sym"}:
                    append_token("O")
                elif local_name in {"instrText", "fldChar"}:
                    append_token("F")
            flush_text_nodes()

    plain_text = "".join(plain_parts)
    if not spans or not plain_text.strip():
        return None
    return LogicalUnit(
        unit_id=f"{part_name}:{unit_index:06d}",
        part_name=part_name,
        spans=spans,
        marked_text="".join(marked_parts),
        plain_text=plain_text,
        expected_tokens=expected_tokens,
    )


def _apply_mapped_translation(unit: LogicalUnit, translated: str) -> bool:
    actual_tokens = _TOKEN_FULL.findall(translated)
    if actual_tokens != unit.expected_tokens:
        return False
    for span in unit.spans:
        start = translated.find(span.start_marker)
        end = translated.find(span.end_marker, start + len(span.start_marker))
        if start < 0 or end < 0:
            return False
        value = translated[start + len(span.start_marker) : end]
        if _TOKEN_FULL.search(value):
            return False
        _set_span_text(span, value)
    return True


def _apply_style_degradation(unit: LogicalUnit, translated: str) -> None:
    first = True
    for span in unit.spans:
        _set_span_text(span, translated if first else "")
        first = False


def _set_span_text(span: RunSpan, value: str) -> None:
    first_node = span.text_nodes[0]
    first_node.text = value
    _set_xml_space(first_node, value)
    for node in span.text_nodes[1:]:
        node.text = ""
        _set_xml_space(node, "")


def _set_xml_space(node: etree._Element, value: str) -> None:
    attribute = f"{{{XML_NS}}}space"
    if value[:1].isspace() or value[-1:].isspace():
        node.set(attribute, "preserve")
    else:
        node.attrib.pop(attribute, None)


def _strip_markers(value: str) -> str:
    return _TOKEN_FULL.sub("", value)
