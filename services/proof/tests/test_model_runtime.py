from pathlib import Path

from proof.config import Settings
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.model_runtime import build_proof_model_runtime


def _settings(pack_id: str, secret_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        model_pack_id=pack_id,
        model_secret_dir=secret_dir,
    )


def test_registered_packs_share_embedding_profile(tmp_path) -> None:
    (tmp_path / "dashscope_api_key").write_text(
        "test-dashscope-key\n",
        encoding="utf-8",
    )
    api_runtime = build_proof_model_runtime(_settings("api-rerank", tmp_path))
    private_runtime = build_proof_model_runtime(
        _settings("api-rerank-similarity", tmp_path)
    )

    assert api_runtime.embedding == private_runtime.embedding
    assert api_runtime.reranker is not None
    assert private_runtime.reranker is not None
    assert api_runtime.reranker.mode == "api"
    assert private_runtime.reranker.mode == "api"
    assert private_runtime.reranker.api_key == "test-dashscope-key"
    assert api_runtime.reranker.instruction != private_runtime.reranker.instruction
    assert (
        OpenAICompatibleEmbeddingClient(api_runtime.embedding).profile
        == OpenAICompatibleEmbeddingClient(private_runtime.embedding).profile
    )
