"""Aliyun IQS web search tool, shaped after PAI-RAG."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger


DEFAULT_ALIYUN_SEARCH_ENDPOINT = "iqs.cn-zhangjiakou.aliyuncs.com"
DEFAULT_SEARCH_COUNT = 10
DEFAULT_TIMERANGE = "OneMonth"
DEFAULT_CONTENT_MAX_LENGTH = 1000
DEFAULT_FAVICON = (
    "https://cdn.pixabay.com/photo/2020/09/17/22/52/"
    "website-5580513_1280.png"
)


@dataclass(slots=True)
class AliyunSearchConfig:
    access_key_id: str
    access_key_secret: str
    endpoint: str = DEFAULT_ALIYUN_SEARCH_ENDPOINT
    search_count: int = DEFAULT_SEARCH_COUNT
    time_range: str = DEFAULT_TIMERANGE


class AliyunSearchTool:
    """Search Aliyun IQS and normalize results for the ReAct agent."""

    def __init__(self, config: AliyunSearchConfig):
        self.config = config
        self.search_count = config.search_count
        self.time_range = config.time_range
        self.client = self._create_client(config)

    @staticmethod
    def _create_client(config: AliyunSearchConfig):
        try:
            from alibabacloud_iqs20241111.client import Client
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError as exc:
            raise ImportError(
                "Aliyun web search requires alibabacloud-iqs20241111 and "
                "alibabacloud-tea-openapi."
            ) from exc

        open_api_config = open_api_models.Config(
            access_key_id=config.access_key_id,
            access_key_secret=config.access_key_secret,
        )
        open_api_config.endpoint = config.endpoint
        return Client(open_api_config)

    async def _search_single_page(self, query: str, page: int) -> dict[str, Any]:
        from alibabacloud_iqs20241111 import models

        request = models.GenericSearchRequest(
            query=query,
            time_range=self.time_range,
            page=page,
        )
        response = await self.client.generic_search_async(request)
        if response.status_code != 200:
            logger.warning(
                "Aliyun Search API failed, status code {}, detail {}",
                response.status_code,
                response,
            )
            return {}
        logger.info("Finished Aliyun search query page {}", page)
        return response.body.to_map()

    async def _asearch(self, query: str) -> list[dict[str, Any]]:
        page_count = 1 + int((self.search_count - 1) / 10)
        tasks = [
            self._search_single_page(query=query, page=page)
            for page in range(1, page_count + 1)
        ]
        page_results = await asyncio.gather(*tasks)

        results: list[dict[str, Any]] = []
        for page_result in page_results:
            for item in page_result.get("pageItems") or []:
                text = (
                    item.get("markdownText")
                    or item.get("mainText")
                    or item.get("htmlSnippet")
                )
                if not text:
                    continue

                results.append(
                    {
                        "content": str(text)[:DEFAULT_CONTENT_MAX_LENGTH],
                        "url": item.get("link"),
                        "title": item.get("title") or item.get("htmlTitle"),
                        "hostname": item.get("hostname"),
                        "favicon": item.get("hostLogo") or DEFAULT_FAVICON,
                        "publish_time": str(item.get("publishTime")),
                        "score": item.get("score") or 0.1,
                    }
                )
                if len(results) >= self.search_count:
                    return results

        return results

    async def aquery(self, query: str) -> dict[str, list[dict[str, Any]] | str]:
        start = time.time()
        logger.info("Aliyun Search with query {}", query)
        try:
            results = await self._asearch(query=query[:400])
        except Exception as exc:
            logger.exception("Error occurred during Aliyun search")
            return {"result": f"Error occurred during Aliyun search: {exc}"}

        logger.info(
            "[WebSearch]-Aliyun: Get {} docs. Elapsed time: {} seconds.",
            len(results),
            time.time() - start,
        )
        return {"result": results}
