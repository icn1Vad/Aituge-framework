"""Retrieval backends for read-only RAG.

The service layer mirrors PAI-RAG's orchestration shape and delegates actual
candidate retrieval to this backend interface. The in-memory implementation is
only a local/dev adapter for already-prepared kb/file/chunk data.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import KnowledgeBase, KnowledgeBaseChunk, KnowledgeBaseFile, RetrievalResult


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_.-]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class RagRetrievalBackend(Protocol):
    def get_knowledgebase(self, kb_id: str) -> KnowledgeBase:
        """Return knowledgebase schema/catalog info."""

    async def text_search(
        self,
        *,
        kb_id: str,
        query: str,
        top_k: int,
    ) -> list[RetrievalResult]:
        """Return fulltext/BM25-style candidates."""

    async def vector_search(
        self,
        *,
        kb_id: str,
        query: str,
        top_k: int,
    ) -> list[RetrievalResult]:
        """Return embedding/vector candidates."""

    async def catalog(self, *, kb_id: str, query: str = "", limit: int = 20) -> dict[str, Any]:
        """List files without body content."""

    async def grep(
        self,
        *,
        kb_id: str,
        pattern: str,
        context: int = 2,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Run exact keyword lookup over file content."""

    async def fetch(
        self,
        *,
        kb_id: str,
        file_id: str | None = None,
        chunk_id: str | None = None,
        offset: int = 0,
        max_chars: int = 6000,
    ) -> dict[str, Any]:
        """Fetch a bounded text window by file/chunk ref."""


def _tokens(text: str) -> list[str]:
    normalized = text.lower()
    return _ASCII_WORD_RE.findall(normalized) + _CJK_RE.findall(normalized)


def _coerce_limit(value: int, *, default: int, maximum: int) -> int:
    if value <= 0:
        return default
    return min(value, maximum)


@dataclass(slots=True)
class InMemoryRagBackend:
    """Local adapter for tests/dev before a real vector or PAI backend exists."""

    knowledgebases: list[KnowledgeBase]
    files: list[KnowledgeBaseFile] = field(default_factory=list)
    chunks: list[KnowledgeBaseChunk] = field(default_factory=list)
    _kb_by_id: dict[str, KnowledgeBase] = field(init=False, default_factory=dict)
    _files_by_kb: dict[str, list[KnowledgeBaseFile]] = field(init=False, default_factory=lambda: defaultdict(list))
    _file_by_id: dict[str, KnowledgeBaseFile] = field(init=False, default_factory=dict)
    _chunks_by_kb: dict[str, list[KnowledgeBaseChunk]] = field(init=False, default_factory=lambda: defaultdict(list))
    _chunks_by_file: dict[str, list[KnowledgeBaseChunk]] = field(init=False, default_factory=lambda: defaultdict(list))
    _chunk_by_id: dict[str, KnowledgeBaseChunk] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._kb_by_id = {kb.id: kb for kb in self.knowledgebases}
        self._files_by_kb = defaultdict(list)
        self._file_by_id = {}
        self._chunks_by_kb = defaultdict(list)
        self._chunks_by_file = defaultdict(list)
        self._chunk_by_id = {}

        for file in self.files:
            if file.kb_id in self._kb_by_id:
                self._files_by_kb[file.kb_id].append(file)
                self._file_by_id[file.id] = file

        for chunk in self.chunks:
            if chunk.kb_id in self._kb_by_id:
                self._chunks_by_kb[chunk.kb_id].append(chunk)
                self._chunks_by_file[chunk.file_id].append(chunk)
                self._chunk_by_id[chunk.id] = chunk

    def get_knowledgebase(self, kb_id: str) -> KnowledgeBase:
        kb = self._kb_by_id.get(kb_id)
        if not kb:
            raise ValueError(f"Knowledgebase {kb_id} not found.")
        return kb

    async def text_search(
        self,
        *,
        kb_id: str,
        query: str,
        top_k: int,
    ) -> list[RetrievalResult]:
        self.get_knowledgebase(kb_id)
        query = query.strip()
        if not query:
            return []

        query_tokens = _tokens(query)
        phrase = query.lower()
        results = []
        for chunk in self._chunks_by_kb.get(kb_id, []):
            file = self._file_by_id.get(chunk.file_id)
            if not file:
                continue
            score = self._keyword_score(
                query_tokens=query_tokens,
                phrase=phrase,
                text=chunk.text,
                title=file.title or file.file_name,
            )
            if score <= 0:
                continue
            metadata = {**dict(file.metadata), **dict(chunk.metadata)}
            results.append(
                RetrievalResult(
                    content=chunk.text,
                    score=score,
                    kb_id=kb_id,
                    chunk_id=chunk.id,
                    file_id=file.id,
                    title=file.title or file.file_name,
                    file_name=file.file_name,
                    url=file.source_url,
                    metadata=metadata,
                )
            )
        return sorted(results, key=lambda item: item.score, reverse=True)[:top_k]

    async def vector_search(
        self,
        *,
        kb_id: str,
        query: str,
        top_k: int,
    ) -> list[RetrievalResult]:
        del query, top_k
        self.get_knowledgebase(kb_id)
        return []

    async def catalog(
        self,
        *,
        kb_id: str,
        query: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        self.get_knowledgebase(kb_id)
        limit = _coerce_limit(limit, default=20, maximum=200)
        query_lower = query.lower().strip()
        results = []
        for file in self._files_by_kb.get(kb_id, []):
            haystack = f"{file.file_name} {file.title or ''}".lower()
            if query_lower and query_lower not in haystack:
                continue
            results.append(
                {
                    "file_id": file.id,
                    "title": file.title or file.file_name,
                    "file_name": file.file_name,
                    "source_url": file.source_url,
                    "status": file.status,
                    "metadata": dict(file.metadata),
                }
            )
            if len(results) >= limit:
                break
        return {"ok": True, "results": results, "total": len(results)}

    async def grep(
        self,
        *,
        kb_id: str,
        pattern: str,
        context: int = 2,
        limit: int = 20,
    ) -> dict[str, Any]:
        self.get_knowledgebase(kb_id)
        pattern = pattern.strip()
        if not pattern:
            return {"ok": True, "results": [], "total": 0}

        context = min(max(context, 0), 10)
        limit = _coerce_limit(limit, default=20, maximum=200)
        pattern_lower = pattern.lower()
        results = []
        for file in self._files_by_kb.get(kb_id, []):
            content = self._file_content(file.id)
            lines = content.splitlines()
            for line_no, line in enumerate(lines, start=1):
                if pattern_lower not in line.lower():
                    continue
                start = max(0, line_no - context - 1)
                end = min(len(lines), line_no + context)
                results.append(
                    {
                        "file_id": file.id,
                        "title": file.title or file.file_name,
                        "file_name": file.file_name,
                        "line": line_no,
                        "match": line,
                        "context": "\n".join(lines[start:end]),
                        "source_url": file.source_url,
                    }
                )
                if len(results) >= limit:
                    return {"ok": True, "results": results, "total": len(results)}
        return {"ok": True, "results": results, "total": len(results)}

    async def fetch(
        self,
        *,
        kb_id: str,
        file_id: str | None = None,
        chunk_id: str | None = None,
        offset: int = 0,
        max_chars: int = 6000,
    ) -> dict[str, Any]:
        self.get_knowledgebase(kb_id)
        file = None
        if chunk_id:
            chunk = self._chunk_by_id.get(chunk_id)
            if chunk and chunk.kb_id == kb_id:
                file = self._file_by_id.get(chunk.file_id)
        if file is None and file_id:
            candidate = self._file_by_id.get(file_id)
            if candidate and candidate.kb_id == kb_id:
                file = candidate
        if file is None:
            return {"ok": False, "error": "not_found", "message": "File not found."}

        content = self._file_content(file.id)
        max_chars = _coerce_limit(max_chars, default=6000, maximum=6000)
        start = max(offset, 0)
        window = content[start:start + max_chars]
        next_offset = start + len(window)
        truncated = next_offset < len(content)
        return {
            "ok": True,
            "file_id": file.id,
            "title": file.title or file.file_name,
            "file_name": file.file_name,
            "source_url": file.source_url,
            "content": window,
            "content_length": len(content),
            "offset": start,
            "returned_chars": len(window),
            "truncated": truncated,
            "next_offset": next_offset if truncated else None,
        }

    @staticmethod
    def _keyword_score(
        *,
        query_tokens: list[str],
        phrase: str,
        text: str,
        title: str,
    ) -> float:
        text_lower = text.lower()
        title_lower = title.lower()
        hits = sum(1 for token in query_tokens if token in text_lower)
        title_hits = sum(1 for token in query_tokens if token in title_lower)
        phrase_bonus = 1.0 if phrase and phrase in text_lower else 0.0
        if hits == 0 and title_hits == 0 and phrase_bonus == 0:
            return 0.0
        raw = hits + title_hits * 1.5 + phrase_bonus * 2.0
        return min(raw / max(len(query_tokens), 1), 1.0)

    def _file_content(self, file_id: str) -> str:
        chunks = sorted(
            self._chunks_by_file.get(file_id, []),
            key=lambda chunk: chunk.index,
        )
        return "\n\n".join(chunk.text for chunk in chunks if chunk.text)
