import asyncio
import json

from data.RAG.tool_retrieval import (
    KnowledgeBase,
    KnowledgeBaseChunk,
    KnowledgeBaseFile,
    RetrievalSettings,
    ToolRetrievalRAG,
)


def _sample_rag():
    return ToolRetrievalRAG(
        knowledgebases=[
            KnowledgeBase(
                id="kb_project",
                name="Project Docs",
                description="Deployment and configuration notes.",
            )
        ],
        files=[
            KnowledgeBaseFile(
                id="file_env",
                kb_id="kb_project",
                file_name="deploy.md",
                title="Deployment Guide",
                source_url="https://example.test/deploy",
            )
        ],
        chunks=[
            KnowledgeBaseChunk(
                id="chunk_redis",
                kb_id="kb_project",
                file_id="file_env",
                index=0,
                text="Configure Redis broker with PAIRAG_BROKER=redis://localhost:6379/0.",
            ),
            KnowledgeBaseChunk(
                id="chunk_poetry",
                kb_id="kb_project",
                file_id="file_env",
                index=1,
                text="Install dependencies with pip install poetry and then poetry install.",
            ),
        ],
        settings=RetrievalSettings(top_k=2),
    )


def test_rag_facade_creates_pai_style_kb_tools():
    tools = _sample_rag().create_tools()

    assert [tool.metadata.name for tool in tools] == [
        "search-knowledgebase-kb_project",
        "catalog-kb_proje",
        "grep-kb_proje",
        "fetch-kb_proje",
    ]


def test_rag_service_search_catalog_grep_fetch():
    async def run():
        service = _sample_rag().create_service()

        records = await service.search(kb_id="kb_project", query="redis broker")
        assert [record.chunk_id for record in records] == ["chunk_redis"]
        assert records[0].score > 0

        catalog = await service.catalog(kb_id="kb_project")
        assert catalog["results"][0]["file_id"] == "file_env"

        grep = await service.grep(kb_id="kb_project", pattern="PAIRAG_BROKER")
        assert grep["results"][0]["line"] == 1

        fetched = await service.fetch(kb_id="kb_project", file_id="file_env")
        assert "poetry install" in fetched["content"]

    asyncio.run(run())


def test_rag_search_tool_returns_json_results():
    async def run():
        search_tool = _sample_rag().create_tools()[0]
        output = await search_tool.acall(query="environment redis")
        payload = json.loads(str(output))

        assert payload["result"][0]["chunk_id"] == "chunk_redis"
        assert payload["result"][0]["title"] == "Deployment Guide"

    asyncio.run(run())

