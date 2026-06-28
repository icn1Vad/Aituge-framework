"""Convenience facade for packaging kb-list based RAG tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from llama_index.core.tools.function_tool import FunctionTool

from .models import (
    KnowledgeBase,
    KnowledgeBaseChunk,
    KnowledgeBaseFile,
    RetrievalSettings,
)
from .rerank import Reranker, ScoreReranker
from .service import RagRetrievalService
from .tools import create_knowledgebase_retrieval_tools


@dataclass(slots=True)
class ToolRetrievalRAG:
    """Package allowed kbs as PAI-style search/catalog/grep/fetch tools."""

    knowledgebases: list[KnowledgeBase]
    files: list[KnowledgeBaseFile] = field(default_factory=list)
    chunks: list[KnowledgeBaseChunk] = field(default_factory=list)
    settings: RetrievalSettings = field(default_factory=RetrievalSettings)
    reranker: Reranker = field(default_factory=ScoreReranker)

    def create_service(self) -> RagRetrievalService:
        return RagRetrievalService.from_in_memory(
            knowledgebases=self.knowledgebases,
            files=self.files,
            chunks=self.chunks,
            default_settings=self.settings,
            reranker=self.reranker,
        )

    def create_tools(self, kb_ids: Iterable[str] | None = None) -> list[FunctionTool]:
        service = self.create_service()
        allowed_ids = list(kb_ids) if kb_ids is not None else [
            kb.id for kb in self.knowledgebases
        ]
        tools: list[FunctionTool] = []
        for kb_id in allowed_ids:
            tools.extend(
                create_knowledgebase_retrieval_tools(
                    service,
                    kb_id=kb_id,
                    settings=self.settings,
                )
            )
        return tools
