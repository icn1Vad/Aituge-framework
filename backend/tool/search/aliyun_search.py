"""Aliyun IQS HTTP web search tool, shaped after PAI-RAG."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger


DEFAULT_ALIYUN_SEARCH_ENDPOINT = "https://cloud-iqs.aliyuncs.com/search/unified"
DEFAULT_SEARCH_COUNT = 10
DEFAULT_ENGINE_TYPE = "LiteAdvanced"
DEFAULT_CONTENT_MAX_LENGTH = 1000
DEFAULT_FAVICON = (
    "https://cdn.pixabay.com/photo/2020/09/17/22/52/"
    "website-5580513_1280.png"
)
DEFAULT_CONTENTS = {
    "mainText": True,
    "markdownText": False,
    "summary": False,
    "rerankScore": True,
}


@dataclass(slots=True)
class AliyunSearchConfig:
    api_key: str
    endpoint: str = DEFAULT_ALIYUN_SEARCH_ENDPOINT
    search_count: int = DEFAULT_SEARCH_COUNT
    engine_type: str = DEFAULT_ENGINE_TYPE
    contents: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONTENTS))
    timeout_seconds: int = 30


class AliyunSearchTool:
    """Search Aliyun IQS over HTTP and normalize results for the ReAct agent."""

    def __init__(self, config: AliyunSearchConfig):
        self.config = config
        self.search_count = config.search_count

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, query: str) -> dict[str, Any]:
        return {
            "query": query[:400],
            "engineType": self.config.engine_type,
            "contents": self.config.contents,
            "advancedParams": {
                "numResults": self.search_count,
            },
        }

    @staticmethod
    def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]]:
        nested_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        return (
            data.get("pageItems")
            or data.get("items")
            or data.get("results")
            or nested_data.get("pageItems")
            or nested_data.get("items")
            or nested_data.get("results")
            or []
        )

    async def _asearch(self, query: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                self.config.endpoint,
                headers=self._headers(),
                json=self._payload(query),
            )

        if response.status_code != 200:
            logger.warning(
                "Aliyun Search API failed, status code {}, detail {}",
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Aliyun Search API failed with status {response.status_code}: "
                f"{response.text[:500]}"
            )

        raw_items = self._extract_items(response.json())
        results: list[dict[str, Any]] = []
        for item in raw_items:
            text = (
                item.get("markdownText")
                or item.get("mainText")
                or item.get("summary")
                or item.get("htmlSnippet")
                or item.get("snippet")
                or item.get("content")
            )
            if not text:
                continue

            results.append(
                {
                    "content": str(text)[:DEFAULT_CONTENT_MAX_LENGTH],
                    "url": item.get("link") or item.get("url"),
                    "title": item.get("title") or item.get("htmlTitle"),
                    "hostname": item.get("hostname") or item.get("hostName"),
                    "favicon": item.get("hostLogo") or item.get("favicon") or DEFAULT_FAVICON,
                    "publish_time": str(item.get("publishTime")),
                    "score": item.get("score") or item.get("rerankScore") or 0.1,
                }
            )
            if len(results) >= self.search_count:
                return results

        return results

    async def aquery(self, query: str) -> dict[str, list[dict[str, Any]] | str]:
        start = time.time()
        logger.info("Aliyun Search with query {}", query)
        try:
            results = await self._asearch(query=query)
        except Exception as exc:
            logger.exception("Error occurred during Aliyun search")
            return {"result": f"Error occurred during Aliyun search: {exc}"}

        logger.info(
            "[WebSearch]-Aliyun: Get {} docs. Elapsed time: {} seconds.",
            len(results),
            time.time() - start,
        )
        return {"result": results}
