from tool.search import AliyunSearchConfig, create_aliyun_web_search_bundle


def test_aliyun_search_bundle_uses_pai_style_tool_name():
    bundle = create_aliyun_web_search_bundle(
        AliyunSearchConfig(
            api_key="fake-api-key",
            search_count=3,
        )
    )

    assert [tool.metadata.name for tool in bundle.tools] == ["aliyun-websearch"]
