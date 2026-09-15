"""Text extraction for the new File resource.

Delegates to the pairag `FileParser` for rich formats (PDF, DOCX, PPTX, MD,
images, videos) so we get the same extraction behaviour as the KB ingestion
pipeline — without re-implementing a dozen readers. Plain text / csv / excel
still have a fast in-process path because they don't need the pairag reader
graph.

Extraction is bounded by ``MAX_EXTRACT_CHARS``. Slicing for clients happens at
read time (``GET /v1/files/{id}/text?offset&limit``), not here.
"""
from io import BytesIO
from typing import BinaryIO, Optional, Tuple

import pandas as pd
from loguru import logger
from backend.attachments.routing import uses_mineru_for_attachment


EXTRACTOR_VERSION = "v5-framework-attachments"

# Per-extraction hard cap. MySQL TEXT caps around 64KB at utf8mb4, but most
# deployments use MEDIUMTEXT; 500KB is comfortable for SQLite and Postgres.
MAX_EXTRACT_CHARS = 500_000

# Fast in-process formats (no pairag FileParser roundtrip).
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".log", ".py", ".js", ".ts", ".html", ".css",
}

# Pairag's FileParser has readers for these. We route everything that isn't a
# trivial text file through it so the PDF / office / markdown pipeline is
# consistent with KB ingestion.
_PAIRAG_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".md",  # richer markdown handling than pure decode()
}


def _cap(text: str) -> Tuple[str, bool]:
    if len(text) > MAX_EXTRACT_CHARS:
        return text[:MAX_EXTRACT_CHARS], True
    return text, False


def _extract_via_pairag(
    raw: bytes,
    file_name: str,
    file_extension: str,
    tenant_id: Optional[str],
) -> Optional[Tuple[str, bool]]:
    """Use pairag's FileParser to turn bytes into plain text.

    Returns None if pairag raised — callers decide whether to swallow the
    failure (e.g. treat the extension as unreadable) or propagate.
    """
    from backend.attachments.vendor.file.models.file_item import FileItem
    from backend.attachments.vendor.file.store.file_store_helper import file_store
    import hashlib
    if file_extension in {".doc", ".ppt"}:
        raise ValueError("旧版 DOC/PPT 请先转换为 DOCX/PPTX 后上传")
    if file_extension == ".docx":
        from backend.attachments.vendor.file.readers.doc_reader import DocxReader
        reader = DocxReader(file_store)
    elif file_extension == ".pptx":
        from backend.attachments.vendor.file.readers.pptx_reader import PptxReader
        reader = PptxReader(file_store)
    elif file_extension == ".md":
        from backend.attachments.vendor.file.readers.markdown_reader import MarkdownReader
        reader = MarkdownReader(file_store)
    else:
        raise ValueError("PDF 解析需要启用 MinerU")
    fi = FileItem(id="attachment", file_path=file_name, file=BytesIO(raw),
                  kb_id="attachments", file_extension=file_extension, file_name=file_name,
                  file_md5=hashlib.md5(raw).hexdigest(), file_size=len(raw), tenant_id=tenant_id or "")
    docs = reader.read(fi)

    if not docs:
        return _cap("")
    body = "\n\n".join((d.text or "").rstrip("\n") for d in docs)
    return _cap(body)


def extract_text(
    file_data: BinaryIO,
    file_extension: str,
    *,
    file_name: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[Tuple[str, bool]]:
    """Extract text from a file. Returns ``(content, truncated_at_extract)``
    or ``None`` for multimodal / unreadable formats.

    - ``.txt`` / code / structured-text: decoded in-process (fast).
    - ``.csv`` / ``.xls[x]``: pandas head-dump (bounded by MAX_EXTRACT_CHARS).
    - ``.pdf`` / ``.docx`` / ``.pptx`` / ``.md``: pairag FileParser.
    - images / videos / everything else: returns ``None`` — the caller serves
      raw bytes via ``/content`` and relies on the multimodal agent tool.
    """
    ext = (file_extension or "").lower()

    if uses_mineru_for_attachment(ext):
        from backend.attachments.service.mineru_extractor import extract_with_mineru
        file_data.seek(0)
        return _cap(extract_with_mineru(file_data.read(), file_name or f"document{ext}"))

    if ext in (".xlsx", ".xls"):
        file_data.seek(0)
        sheets = pd.read_excel(file_data, sheet_name=None)
        return _cap("\n\n".join(f"## {name}\n{df.to_csv(index=False)}" for name, df in sheets.items()))

    if ext == ".csv":
        file_data.seek(0)
        df = pd.read_csv(file_data)
        return _cap(df.to_csv(index=False))

    if ext in _TEXT_EXTENSIONS and ext != ".md":
        file_data.seek(0)
        raw = file_data.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        return _cap(text)

    if ext in _PAIRAG_EXTENSIONS:
        file_data.seek(0)
        raw = file_data.read()
        return _extract_via_pairag(
            raw=raw,
            file_name=file_name or f"document{ext}",
            file_extension=ext,
            tenant_id=tenant_id,
        )

    return None


def extract_text_from_bytes(
    raw: bytes,
    file_extension: str,
    *,
    file_name: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[Tuple[str, bool]]:
    return extract_text(
        BytesIO(raw),
        file_extension,
        file_name=file_name,
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# Chunking — used by process_file_resource_task to populate pai_file_chunk.
# ---------------------------------------------------------------------------

DEFAULT_CHUNK_SIZE = 500         # characters per chunk
DEFAULT_CHUNK_OVERLAP = 50       # characters shared between adjacent chunks

# Files below this extracted-text length are served inline only — no chunking,
# no search tool. Matches the agent's LLM_INLINE_TEXT_LIMIT so small files
# don't pay the chunking cost.
SEARCHABLE_MIN_CHARS = 5_000


def chunk_text(
    content: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict]:
    """Split ``content`` into overlapping windows suitable for keyword search.

    Each returned dict is ``{index, content, start, end}``. Simple character-
    based sliding window — prioritises predictability and zero model deps.
    Sentence-boundary splitting can come later if we find it helps retrieval.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")
    if not content:
        return []

    step = chunk_size - overlap
    chunks: list[dict] = []
    start = 0
    idx = 0
    total = len(content)
    while start < total:
        end = min(start + chunk_size, total)
        body = content[start:end]
        chunks.append({
            "index": idx,
            "content": body,
            "start": start,
            "end": end,
        })
        if end >= total:
            break
        start += step
        idx += 1
    return chunks


def should_chunk(content_length: int) -> bool:
    """Chunks are only produced when the extracted text is big enough that an
    agent would plausibly need to search within it instead of reading in full.
    """
    return content_length >= SEARCHABLE_MIN_CHARS
