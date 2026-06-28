"""Shared schema for read-only RAG retrieval.

These models intentionally describe the query side only. They do not own file
upload, parsing, chunking, embedding, or index maintenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


RetrievalMode = Literal["keyword", "vector", "hybrid"]


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    id: str
    name: str
    description: str = ""
    metadata_schema: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class KnowledgeBaseFile:
    id: str
    kb_id: str
    file_name: str
    title: str | None = None
    source_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    status: str = "completed"


@dataclass(frozen=True, slots=True)
class KnowledgeBaseChunk:
    id: str
    kb_id: str
    file_id: str
    text: str
    index: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    retrieval_mode: RetrievalMode = "keyword"
    top_k: int = 5
    similarity_threshold: float = 0.0
    vector_weight: float = 0.5
    enable_rerank: bool = False
    rerank_top_k: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    content: str
    score: float
    kb_id: str
    chunk_id: str
    file_id: str
    title: str
    file_name: str
    url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "kb_id": self.kb_id,
            "chunk_id": self.chunk_id,
            "file_id": self.file_id,
            "title": self.title,
            "file_name": self.file_name,
            "url": self.url,
            "metadata": dict(self.metadata),
        }

