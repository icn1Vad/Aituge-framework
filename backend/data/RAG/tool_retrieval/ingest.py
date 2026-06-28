"""Non-agent ingestion helpers for local RAG retrieval tests.

This module deliberately does not expose AI tools. It prepares a small local
kb/file/chunk store so the retrieval tools can be tested end to end.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import KnowledgeBase, KnowledgeBaseChunk, KnowledgeBaseFile


DEFAULT_STORE_PATH = Path(__file__).resolve().parent / "store" / "kb_store.json"
DEFAULT_UPLOAD_DIR = Path(__file__).resolve().parent / "store" / "uploads"
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 120


def _clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_with_pdftotext(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def extract_pdf_text(path: Path) -> str:
    """Extract PDF text with optional Python libs, falling back to pdftotext."""

    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        return _clean_text("\n\n".join(page.extract_text() or "" for page in reader.pages))
    except ModuleNotFoundError:
        pass

    try:
        from PyPDF2 import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        return _clean_text("\n\n".join(page.extract_text() or "" for page in reader.pages))
    except ModuleNotFoundError:
        pass

    return _clean_text(_extract_with_pdftotext(path))


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    if chunk_size <= chunk_overlap:
        raise ValueError("chunk_size must be greater than chunk_overlap.")

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        window = text[start:end].strip()
        if window:
            chunks.append(window)
        if end >= len(text):
            break
        start = end - chunk_overlap
    return chunks


@dataclass(slots=True)
class LocalRagStore:
    store_path: Path = DEFAULT_STORE_PATH
    upload_dir: Path = DEFAULT_UPLOAD_DIR

    def _empty(self) -> dict[str, Any]:
        return {
            "next_kb_id": 1,
            "next_file_id": 1,
            "next_chunk_id": 1,
            "knowledgebases": [],
            "files": [],
            "chunks": [],
        }

    def load(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return self._empty()
        try:
            return json.loads(self.store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return self._empty()

    def save(self, data: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.store_path)

    def load_models(self) -> tuple[list[KnowledgeBase], list[KnowledgeBaseFile], list[KnowledgeBaseChunk]]:
        data = self.load()
        knowledgebases = [KnowledgeBase(**item) for item in data.get("knowledgebases", [])]
        files = [KnowledgeBaseFile(**item) for item in data.get("files", [])]
        chunks = [KnowledgeBaseChunk(**item) for item in data.get("chunks", [])]
        return knowledgebases, files, chunks

    def ingest_pdf(
        self,
        path: Path,
        *,
        kb_name: str = "kb_1",
        kb_description: str = "Local PDF knowledgebase.",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        copy_source: bool = True,
    ) -> dict[str, Any]:
        path = Path(path).expanduser().resolve()
        text = extract_pdf_text(path)
        chunks = chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if not chunks:
            raise ValueError(f"No extractable text found in PDF: {path}")

        data = self.load()
        kb_id = str(data.get("next_kb_id", 1))
        file_id = str(data.get("next_file_id", 1))
        first_chunk_id = int(data.get("next_chunk_id", 1))

        stored_path = str(path)
        if copy_source:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            target = self.upload_dir / f"{file_id}.pdf"
            shutil.copy2(path, target)
            stored_path = str(target)

        kb = KnowledgeBase(
            id=kb_id,
            name=kb_name,
            description=kb_description,
        )
        file = KnowledgeBaseFile(
            id=file_id,
            kb_id=kb_id,
            file_name=path.name,
            title=path.stem,
            source_url=stored_path,
            metadata={"source_path": str(path)},
        )
        chunk_entities = [
            KnowledgeBaseChunk(
                id=str(first_chunk_id + index),
                kb_id=kb_id,
                file_id=file_id,
                text=chunk,
                index=index,
                metadata={"source_path": str(path)},
            )
            for index, chunk in enumerate(chunks)
        ]

        data["knowledgebases"].append(asdict(kb))
        data["files"].append(asdict(file))
        data["chunks"].extend(asdict(chunk) for chunk in chunk_entities)
        data["next_kb_id"] = int(kb_id) + 1
        data["next_file_id"] = int(file_id) + 1
        data["next_chunk_id"] = first_chunk_id + len(chunk_entities)
        self.save(data)

        return {
            "ok": True,
            "kb_id": kb_id,
            "file_id": file_id,
            "chunk_count": len(chunk_entities),
            "text_length": len(text),
            "file_name": path.name,
        }

    async def ingest_upload(self, file_name: str, content: bytes) -> dict[str, Any]:
        suffix = Path(file_name).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            return self.ingest_pdf(
                tmp_path,
                kb_name="kb_1",
                kb_description="Uploaded local PDF knowledgebase.",
                copy_source=True,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

