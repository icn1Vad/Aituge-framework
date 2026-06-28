"""FunctionTool wrappers for PAI-style knowledgebase retrieval."""

from __future__ import annotations

import json

from llama_index.core.tools import FunctionTool

from .models import RetrievalSettings
from .service import RagRetrievalService


def create_knowledgebase_retrieval_tools(
    service: RagRetrievalService,
    *,
    kb_id: str,
    settings: RetrievalSettings | None = None,
) -> list[FunctionTool]:
    """Create search/catalog/grep/fetch tools for one allowed knowledgebase."""

    kb = service.get_knowledgebase(kb_id)
    settings = settings or service.default_settings

    async def search_handler(query: str = "") -> str:
        records = await service.search(kb_id=kb_id, query=query, settings=settings)
        return json.dumps(
            {"result": [record.to_dict() for record in records]},
            ensure_ascii=False,
        )

    search_tool = FunctionTool.from_defaults(
        async_fn=search_handler,
        name=f"search-knowledgebase-{kb_id[:10]}",
        description=(
            "Search the private knowledgebase for information relevant to the "
            "user's query.\n\n"
            "# Knowledgebase info\n"
            f"- Name: {kb.name}\n"
            f"- Description: {kb.description}\n\n"
            "# Parameters\n"
            "- query: standalone search query.\n\n"
            "# Returns\n"
            "- JSON list of matched chunks with content, metadata, source, and score."
        ),
        return_direct=False,
    )

    async def catalog_handler(query: str = "", limit: int = 20) -> str:
        result = await service.catalog(kb_id=kb_id, query=query, limit=limit)
        return json.dumps(result, ensure_ascii=False)

    catalog_tool = FunctionTool.from_defaults(
        async_fn=catalog_handler,
        name=f"catalog-{kb_id[:8]}",
        description=(
            f"List files in knowledgebase '{kb.name}' without reading file bodies. "
            "Use it to inspect available files before grep/fetch."
        ),
        return_direct=False,
    )

    async def grep_handler(
        pattern: str = "",
        context: int = 2,
        limit: int = 20,
    ) -> str:
        result = await service.grep(
            kb_id=kb_id,
            pattern=pattern,
            context=context,
            limit=limit,
        )
        return json.dumps(result, ensure_ascii=False)

    grep_tool = FunctionTool.from_defaults(
        async_fn=grep_handler,
        name=f"grep-{kb_id[:8]}",
        description=(
            f"Exact keyword lookup across files in knowledgebase '{kb.name}'. "
            "Use it for identifiers, error codes, config keys, and exact phrases."
        ),
        return_direct=False,
    )

    async def fetch_handler(
        file_id: str | None = None,
        chunk_id: str | None = None,
        offset: int = 0,
        max_chars: int = 6000,
    ) -> str:
        result = await service.fetch(
            kb_id=kb_id,
            file_id=file_id,
            chunk_id=chunk_id,
            offset=offset,
            max_chars=max_chars,
        )
        return json.dumps(result, ensure_ascii=False)

    fetch_tool = FunctionTool.from_defaults(
        async_fn=fetch_handler,
        name=f"fetch-{kb_id[:8]}",
        description=(
            f"Fetch a text window from a file in knowledgebase '{kb.name}'. "
            "Use it when snippets are too sparse and a larger passage is needed."
        ),
        return_direct=False,
    )

    return [search_tool, catalog_tool, grep_tool, fetch_tool]
