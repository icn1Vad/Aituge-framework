from __future__ import annotations

import hashlib
import re
from typing import Any


HEADING = re.compile(r"^(?:第[一二三四五六七八九十百]+[章节篇部]|[一二三四五六七八九十]+、|\d+(?:\.\d+)*[、.])\s*\S+")


def build_chunks(document_id: str, pages: list[dict[str, Any]], *, max_chars: int, overlap: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    ordinal = 0
    current_heading = ""
    for page in pages:
        page_no = int(page["page_no"])
        text = normalize_text(str(page.get("effective_text") or ""))
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            if len(line) <= 80 and HEADING.match(line):
                current_heading = line
                break
        start = 0
        while start < len(text):
            end = min(len(text), start + max_chars)
            if end < len(text):
                boundary = max(text.rfind(mark, start + max_chars // 2, end) for mark in ("。", "；", "！", "？", "\n"))
                if boundary > start:
                    end = boundary + 1
            content = text[start:end].strip()
            if content:
                ordinal += 1
                digest = hashlib.sha256(f"{document_id}|{page_no}|{ordinal}|{content}".encode()).hexdigest()
                parent_start = max(0, start - max_chars // 2)
                parent_end = min(len(text), end + max_chars // 2)
                chunks.append({
                    "id": f"qxs-chunk-{digest[:24]}", "ordinal": ordinal,
                    "chapter": current_heading, "heading_path": [current_heading] if current_heading else [],
                    "page_start": page_no, "page_end": page_no, "content": content,
                    "parent_content": text[parent_start:parent_end].strip(),
                    "text_hash": hashlib.sha256(content.encode()).hexdigest(),
                })
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks


def normalize_text(text: str) -> str:
    text = text.replace("\u0000", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
