from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pypdf import PdfReader


WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SUPPORTED_EXTENSIONS = {".pdf", ".docx"}


class SmartFillDocumentStore:
    """File-backed single-user document store with stable source anchors."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.getenv("SMART_FILL_DOCUMENT_DIR")
        self.root = Path(configured) if configured else Path(__file__).parents[1] / "data" / "smart_autofill"
        self.files_dir = self.root / "files"
        self.parsed_dir = self.root / "parsed"

    def ingest(self, file_name: str, content: bytes) -> dict[str, Any]:
        extension = Path(file_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported document type '{extension}'. Expected PDF or DOCX.")
        if not content:
            raise ValueError("Uploaded document is empty.")
        digest = hashlib.sha256(content).hexdigest()
        document_id = f"doc_{digest[:24]}"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        source_path = self.files_dir / f"{document_id}{extension}"
        source_path.write_bytes(content)
        parsed = self._parse(document_id, file_name, extension, content, digest)
        (self.parsed_dir / f"{document_id}.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.summary(parsed)

    def get(self, document_id: str) -> dict[str, Any]:
        path = self.parsed_dir / f"{document_id}.json"
        if not path.exists():
            raise KeyError(document_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def summary(self, parsed: dict[str, Any]) -> dict[str, Any]:
        return {key: parsed[key] for key in (
            "document_id", "file_name", "extension", "sha256", "size_bytes",
            "page_count", "paragraph_count", "table_count", "text_chars", "quality",
        )}

    def _parse(self, document_id: str, file_name: str, extension: str, content: bytes, digest: str):
        if extension == ".pdf":
            chunks, page_count = _parse_pdf(document_id, file_name, content)
        else:
            chunks, page_count = _parse_docx(document_id, file_name, content)
        text_chars = sum(len(chunk["text"]) for chunk in chunks)
        paragraph_count = sum(chunk["kind"] == "paragraph" for chunk in chunks)
        table_count = sum(chunk["kind"] == "table" for chunk in chunks)
        empty_chunks = sum(not chunk["text"].strip() for chunk in chunks)
        return {
            "schema_version": "smart-fill-document-v1",
            "document_id": document_id,
            "file_name": file_name,
            "extension": extension,
            "sha256": digest,
            "size_bytes": len(content),
            "page_count": page_count,
            "paragraph_count": paragraph_count,
            "table_count": table_count,
            "text_chars": text_chars,
            "quality": {
                "status": "usable" if text_chars >= 100 else "low_text",
                "text_extracted": text_chars > 0,
                "empty_chunk_count": empty_chunks,
                "warnings": [] if text_chars >= 100 else ["Very little extractable text was found."],
            },
            "chunks": chunks,
        }


def _parse_pdf(document_id: str, file_name: str, content: bytes):
    reader = PdfReader(io.BytesIO(content))
    chunks = []
    global_offset = 0
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        chunks.append(_chunk(document_id, file_name, len(chunks), "page", text,
                             page=index, char_start=global_offset, char_end=global_offset + len(text)))
        global_offset += len(text) + 1
    return chunks, len(reader.pages)


def _parse_docx(document_id: str, file_name: str, content: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("The DOCX package is invalid or cannot be parsed.") from exc
    body = root.find(f"{WORD_NS}body")
    chunks: list[dict[str, Any]] = []
    paragraph_index = 0
    table_index = 0
    section = None
    global_offset = 0
    for node in list(body or []):
        if node.tag == f"{WORD_NS}p":
            text = _node_text(node).strip()
            style = node.find(f"{WORD_NS}pPr/{WORD_NS}pStyle")
            style_value = style.get(f"{WORD_NS}val", "") if style is not None else ""
            if text and (style_value.lower().startswith("heading") or re.match(r"^\d+(?:\.\d+)*[、.\s]", text)):
                section = text
            if text:
                chunks.append(_chunk(document_id, file_name, len(chunks), "paragraph", text,
                                     section=section, paragraph_index=paragraph_index,
                                     char_start=global_offset, char_end=global_offset + len(text)))
                global_offset += len(text) + 1
            paragraph_index += 1
        elif node.tag == f"{WORD_NS}tbl":
            rows = []
            for row in node.findall(f"{WORD_NS}tr"):
                rows.append([_node_text(cell).strip() for cell in row.findall(f"{WORD_NS}tc")])
            text = "\n".join(" | ".join(cells) for cells in rows).strip()
            chunks.append(_chunk(document_id, file_name, len(chunks), "table", text,
                                 section=section, table_index=table_index, rows=rows,
                                 char_start=global_offset, char_end=global_offset + len(text)))
            global_offset += len(text) + 1
            table_index += 1
    return chunks, None


def _node_text(node: ET.Element) -> str:
    return "".join(text.text or "" for text in node.iter(f"{WORD_NS}t"))


def _chunk(document_id: str, file_name: str, index: int, kind: str, text: str, **anchor):
    return {
        "chunk_id": f"{document_id}:{kind}:{index}",
        "document_id": document_id,
        "file_name": file_name,
        "kind": kind,
        "index": index,
        "text": text,
        "anchor": {key: value for key, value in anchor.items() if value is not None},
    }
