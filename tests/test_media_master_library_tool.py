import asyncio
import json

import httpx

from tool.media_master_library import factory


def test_media_master_library_tools_use_fixed_gateway_routes(monkeypatch):
    requests = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode()) if request.content else None
        requests.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/items/role_yanjie"):
            return httpx.Response(200, json={"ok": True, "item": {"id": "role_yanjie"}})
        return httpx.Response(200, json={"ok": True, "items": []})

    def create_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr(factory.httpx, "AsyncClient", create_client)
    tools = factory.create_media_master_library_tools(
        factory.MediaMasterLibraryConfig(base_url="http://media.internal")
    )
    by_name = {tool.metadata.name: tool for tool in tools}

    async def run():
        search = await by_name["media_search_master_library"].acall(
            query="退役 证书",
            library_types=["role"],
            top_k=3,
        )
        fetched = await by_name["media_fetch_master_library_item"].acall(
            item_id="role_yanjie"
        )
        return json.loads(search.content), json.loads(fetched.content)

    search_result, fetched_result = asyncio.run(run())

    assert search_result == {"ok": True, "items": []}
    assert fetched_result["item"]["id"] == "role_yanjie"
    assert requests[0] == (
        "POST",
        "/master-library/search",
        {
            "query": "退役 证书",
            "library_types": ["role"],
            "tags": [],
            "top_k": 3,
            "include_full": False,
        },
    )
    assert requests[1][:2] == ("GET", "/master-library/items/role_yanjie")
