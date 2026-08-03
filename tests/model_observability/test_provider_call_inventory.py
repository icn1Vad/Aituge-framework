from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).parents[2]
INVENTORY_PATH = (
    ROOT / "backend" / "model_observability" / "provider_call_inventory.v1.json"
)


def _tree(path: str) -> ast.AST:
    return ast.parse((ROOT / path).read_text("utf-8"))


def _chain(node: ast.AST) -> str:
    values: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        values.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        values.append(current.id)
    return ".".join(reversed(values))


def _calls_ending(path: str, endings: tuple[str, ...]) -> int:
    return sum(
        1
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and any(_chain(node.func).endswith(ending) for ending in endings)
    )


def _constructor_calls(name: str) -> list[str]:
    found: list[str] = []
    for path in (ROOT / "backend").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
            for node in ast.walk(tree)
        ):
            found.append(path.relative_to(ROOT).as_posix())
    return sorted(found)


def test_provider_call_inventory_matches_production_ast_and_reachability() -> None:
    document = json.loads(INVENTORY_PATH.read_text("utf-8"))
    assert document["schemaVersion"] == "1.1"
    assert document["generatedFrom"] == "page2-owned-model-boundary-ast"
    assert "out of scope" in document["inventoryScope"]

    boundaries = {item["id"]: item for item in document["boundaries"]}
    assert set(boundaries) == {
        "legacy-openai-adapter",
        "pai-llm-provider",
        "llm-runtime-provider",
        "tool-retrieval-reranker",
        "proof-embedding-provider",
        "proof-reranker-provider",
        "proof-offline-conflict-judge",
        "proof-offline-atomic-retrieval",
        "proof-offline-atomic-validation",
        "proof-offline-conflict-ab",
        "contract-revision-observability-proxy",
    }

    actual_counts = {
        boundary_id: _calls_ending(
            boundary["path"],
            tuple(boundary["astCallEndings"]),
        )
        for boundary_id, boundary in boundaries.items()
    }
    assert {
        key: value["directCallCount"] for key, value in boundaries.items()
    } == actual_counts

    proof_ids = {
        "proof-embedding-provider",
        "proof-reranker-provider",
        "proof-offline-conflict-judge",
        "proof-offline-atomic-retrieval",
        "proof-offline-atomic-validation",
        "proof-offline-conflict-ab",
    }
    inventoried_proof_paths = sorted(boundaries[item]["path"] for item in proof_ids)
    proof_httpx_post_paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "services" / "proof").rglob("*.py")
        if _calls_ending(path.relative_to(ROOT).as_posix(), ("httpx.post",))
    )
    assert inventoried_proof_paths == proof_httpx_post_paths

    for boundary_id in proof_ids:
        boundary = boundaries[boundary_id]
        source = (ROOT / boundary["path"]).read_text("utf-8")
        assert boundary["status"] == "COVERED_BY_PROOF_OBSERVER"
        if boundary["executionScope"] == "OFFLINE_TOOL":
            assert "observed_tool_attempt(" in source
        else:
            assert "self.observer.begin(" in source
        assert "observation.dispatched(" in source
        assert "observation.succeeded(" in source

    pai_constructors = [
        path
        for path in _constructor_calls("PaiLlm")
        if not path.endswith("/common/llm/llm_model.py")
    ]
    assert pai_constructors == [
        "backend/single-agent/service/conversation/llm_runner.py"
    ]
    assert _constructor_calls("OpenAICompatibleReranker") == []
    assert _constructor_calls("OpenAILike") == []

    runner = (
        ROOT
        / "backend"
        / "single-agent"
        / "service"
        / "agent"
        / "single_agent_runner.py"
    ).read_text("utf-8")
    assert "self.llm_runtime = LlmRuntime" in runner
    assert "llm_runtime=self.llm_runtime" in runner

    facade = (
        ROOT / "backend" / "data" / "RAG" / "tool_retrieval" / "facade.py"
    ).read_text("utf-8")
    assert "ScoreReranker" in facade
    assert boundaries["legacy-openai-adapter"]["status"] == "UNWIRED_RED"
    assert boundaries["tool-retrieval-reranker"]["status"] == "UNWIRED_RED"

    proxy = boundaries["contract-revision-observability-proxy"]
    assert proxy["boundaryType"] == "INTERNAL_OBSERVABILITY_PROXY"
    assert proxy["status"] == "INTERNAL_PROXY_NOT_PROVIDER"
    proxy_source = (ROOT / proxy["path"]).read_text("utf-8")
    assert "/v1/internal/contract-revision-drafts:complete" in proxy_source
    assert "/v1/internal/contract-revision-drafts:finalize" in proxy_source
    assert "finalize_token" in proxy_source
