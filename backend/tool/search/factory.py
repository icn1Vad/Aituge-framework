"""Factories for web search FunctionTools."""

from __future__ import annotations

import json

from llama_index.core.tools import FunctionTool

from tool.bundle import ToolBundle

from .aliyun_search import AliyunSearchConfig, AliyunSearchTool


def create_aliyun_web_search_tools(
    config: AliyunSearchConfig,
) -> list[FunctionTool]:
    search_client = AliyunSearchTool(config)

    async def aget_search_result(query: str) -> str:
        result = await search_client.aquery(query)
        return json.dumps(result, ensure_ascii=False)

    return [
        FunctionTool.from_defaults(
            async_fn=aget_search_result,
            name="aliyun-websearch",
            description="""Search the web with Aliyun IQS for up-to-date information and return relevant results.

# When to use
- Current events, news, or recent happenings.
- Real-time or changing facts such as product info, policies, companies, people, and technical docs.
- Any topic where fresh external information would improve the answer.

# Parameters
- query (required, string): A clear, specific search query. Rewrite vague references into standalone queries and include date/time words for time-sensitive topics.

# Returns
- JSON object: {"result": [{"title": "...", "url": "...", "content": "...", "score": 0.1, ...}]}
""",
            return_direct=False,
        )
    ]


def create_aliyun_web_search_bundle(config: AliyunSearchConfig) -> ToolBundle:
    return ToolBundle.from_tools(create_aliyun_web_search_tools(config))
