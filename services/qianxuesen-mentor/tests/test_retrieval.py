from qianxuesen_mentor.retrieval import reciprocal_rank_fusion, route_quotas


def test_query_routing_changes_layer_quota():
    assert route_quotas("钱学森何时回国？", 12)["fact"] > route_quotas("如何做系统工程？", 12)["fact"]
    assert route_quotas("请给出原话出处", 12)["book"] >= 8
    assert route_quotas("如何做总体设计？", 12)["principle"] >= 5


def test_rrf_deduplicates_same_evidence():
    first = {"id": "1", "content": "a"}
    result = reciprocal_rank_fusion([[first], [first, {"id": "2", "content": "b"}]])
    assert [item["id"] for item in result] == ["1", "2"]
