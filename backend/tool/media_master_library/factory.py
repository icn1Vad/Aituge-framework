"""FunctionTool factory for the media_military master-library Gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from llama_index.core.tools import FunctionTool

from tool.bundle import ToolBundle


@dataclass(frozen=True, slots=True)
class MediaMasterLibraryConfig:
    base_url: str
    timeout_seconds: float = 30.0
    auth_token: str = ""

    def __post_init__(self) -> None:
        normalized = self.base_url.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("media master-library base_url must use http or https")
        object.__setattr__(self, "base_url", normalized)


class MediaMasterLibraryClient:
    def __init__(self, config: MediaMasterLibraryConfig) -> None:
        self.config = config

    async def get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, payload)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            headers=headers,
        ) as client:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise ValueError("media master-library Gateway returned a non-object response")
        return result


def _json_result(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_media_master_library_tools(
    config: MediaMasterLibraryConfig,
) -> list[FunctionTool]:
    client = MediaMasterLibraryClient(config)

    async def media_get_master_library_manifest(
        library_type: str = "",
        item_limit: int = 20,
    ) -> str:
        return _json_result(
            await client.post(
                "/master-library/manifest",
                {
                    "library_type": library_type or None,
                    "item_limit": item_limit,
                },
            )
        )

    async def media_search_master_library(
        query: str,
        library_types: list[str] | None = None,
        tags: list[str] | None = None,
        top_k: int = 8,
    ) -> str:
        return _json_result(
            await client.post(
                "/master-library/search",
                {
                    "query": query,
                    "library_types": library_types,
                    "tags": tags or [],
                    "top_k": top_k,
                    "include_full": False,
                },
            )
        )

    async def media_fetch_master_library_item(item_id: str) -> str:
        return _json_result(
            await client.get(f"/master-library/items/{quote(item_id, safe='')}")
        )

    async def media_recommend_templates_for_strategy(
        strategy_id: str = "",
        strategy_title: str = "",
        query: str = "",
        top_k: int = 8,
    ) -> str:
        return _json_result(
            await client.post(
                "/master-library/templates/by-strategy",
                {
                    "strategy_id": strategy_id,
                    "strategy_title": strategy_title,
                    "query": query,
                    "top_k": top_k,
                },
            )
        )

    async def media_get_random_script_type_candidates(
        top_k: int = 10,
        exclude_ids: list[str] | None = None,
    ) -> str:
        return _json_result(
            await client.post(
                "/master-library/script-types/random",
                {
                    "top_k": top_k,
                    "exclude_ids": exclude_ids or [],
                },
            )
        )

    async def media_get_script_examples_by_strategy(
        strategy_id: str = "",
        strategy_title: str = "",
        top_k: int = 5,
        include_full: bool = False,
    ) -> str:
        return _json_result(
            await client.post(
                "/master-library/script-examples/by-strategy",
                {
                    "strategy_id": strategy_id,
                    "strategy_title": strategy_title,
                    "top_k": top_k,
                    "include_full": include_full,
                },
            )
        )

    return [
        FunctionTool.from_defaults(
            async_fn=media_get_master_library_manifest,
            name="media_get_master_library_manifest",
            description=(
                "Inspect compact master-library coverage before selecting cards. "
                "Optionally restrict to one library_type."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=media_search_master_library,
            name="media_search_master_library",
            description=(
                "Search compact role, strategy, risk-rule, prompt-skill, or example candidates. "
                "Use dedicated tools for templates and script types."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=media_fetch_master_library_item,
            name="media_fetch_master_library_item",
            description=(
                "Fetch the full content of one selected master-library card by id. "
                "Fetch only cards likely to be adopted."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=media_recommend_templates_for_strategy,
            name="media_recommend_templates_for_strategy",
            description=(
                "Recommend template candidates that fit the final selected strategy. "
                "Call only after choosing a strategy."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=media_get_random_script_type_candidates,
            name="media_get_random_script_type_candidates",
            description=(
                "Return a random batch of script-type candidates. Pass exclude_ids to request a new batch."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=media_get_script_examples_by_strategy,
            name="media_get_script_examples_by_strategy",
            description=(
                "Return script examples associated with the final selected strategy. "
                "Use examples as structural references, not text to copy."
            ),
        ),
    ]


def create_media_master_library_bundle(
    config: MediaMasterLibraryConfig,
) -> ToolBundle:
    return ToolBundle.from_tools(create_media_master_library_tools(config))
