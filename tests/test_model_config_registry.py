import asyncio
from pathlib import Path

import yaml

from aituge_model_config import (
    ModelRuntimeProvider,
    ResolvedLlmModel,
    SecretResolver,
    get_model_pack_for_ai_mode,
    load_model_registry,
)
from service.conversation.llm_runner import LlmRuntime


def test_api_modes_share_llm_and_embedding_but_change_rerank_instruction() -> None:
    registry = load_model_registry()
    public_pack = registry.resolve_pack("api-rerank")
    private_pack = registry.resolve_pack("api-rerank-similarity")

    assert public_pack.llm == private_pack.llm
    assert public_pack.embedding == private_pack.embedding
    assert public_pack.reranker.id == "api-qwen3-rerank"
    assert private_pack.reranker.id == "api-qwen3-rerank-similarity"
    assert public_pack.reranker.mode == private_pack.reranker.mode == "api"
    assert public_pack.reranker.instruction != private_pack.reranker.instruction


def test_ai_modes_resolve_to_registered_packages() -> None:
    assert get_model_pack_for_ai_mode("public").id == "api-rerank"
    assert get_model_pack_for_ai_mode("private").id == "api-rerank-similarity"


def test_ai_mode_package_mapping_can_be_overridden() -> None:
    environment = {
        "MODEL_PACK_PUBLIC_ID": "api-rerank-similarity",
        "MODEL_PACK_PRIVATE_ID": "api-rerank",
    }

    assert get_model_pack_for_ai_mode("public", environment=environment).id == "api-rerank-similarity"
    assert get_model_pack_for_ai_mode("private", environment=environment).id == "api-rerank"


def test_secret_resolver_uses_reference_without_storing_secret_in_pack(tmp_path) -> None:
    secret_file = tmp_path / "dashscope_api_key"
    secret_file.write_text("from-file\n", encoding="utf-8")
    resolver = SecretResolver(secret_dir=tmp_path, environment={})

    assert resolver.resolve("dashscope_api_key", required=True) == "from-file"


def test_runtime_provider_defaults_secrets_to_selected_config_directory(
    tmp_path,
) -> None:
    components = {
        "version": 1,
        "default_pack_id": "portable",
        "llms": {
            "portable-llm": {
                "mode": "api",
                "provider": "openai_compatible",
                "model": "portable-llm",
                "base_url": "https://example.invalid/v1",
                "credential_ref": "portable_api_key",
            }
        },
        "embeddings": {
            "portable-embedding": {
                "mode": "local",
                "provider": "openai_compatible",
                "model": "portable-embedding",
                "base_url": "http://embedding:8000/v1",
                "dimensions": 32,
            }
        },
        "rerankers": {
            "portable-reranker": {
                "mode": "local",
                "provider": "openai_compatible",
                "model": "portable-reranker",
                "base_url": "http://reranker:8000/v1/reranks",
            }
        },
    }
    (tmp_path / "components.yaml").write_text(
        yaml.safe_dump(components),
        encoding="utf-8",
    )
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "portable.yaml").write_text(
        "\n".join(
            (
                "id: portable",
                "display_name: Portable",
                "llm: portable-llm",
                "embedding: portable-embedding",
                "reranker: portable-reranker",
            )
        ),
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "portable_api_key").write_text(
        "portable-secret\n",
        encoding="utf-8",
    )

    provider = ModelRuntimeProvider.from_environment(directory=tmp_path)

    assert provider.secret_resolver.secret_dir == Path(tmp_path) / "secrets"
    assert provider.resolve_llm().api_key == "portable-secret"


def test_runtime_provider_resolves_llm_from_registry_with_credential_fallback(
    tmp_path,
) -> None:
    provider = ModelRuntimeProvider.from_environment(
        secret_dir=tmp_path,
        secret_overrides={},
    )

    resolved = provider.resolve_llm(
        "deepseek-v4-pro",
        credential_fallback="legacy-database-credential",
    )

    assert resolved.base_url == "https://api.deepseek.com"
    assert resolved.model == "deepseek-v4-pro"
    assert resolved.provider == "deepseek"
    assert resolved.api_key == "legacy-database-credential"


def test_single_agent_gets_llm_metadata_from_shared_runtime_provider(
    monkeypatch,
    tmp_path,
) -> None:
    provider = ModelRuntimeProvider.from_environment(
        secret_dir=tmp_path,
        secret_overrides={},
    )
    runtime = LlmRuntime(
        tenant_id="tenant-1",
        llm_factory=lambda config: config,
        model_runtime_provider=provider,
    )

    async def legacy_credential(_model_id: str) -> str:
        return "legacy-database-credential"

    monkeypatch.setattr(runtime, "_legacy_database_credential", legacy_credential)
    resolved = asyncio.run(runtime.get_llm("deepseek-v4-pro"))

    assert isinstance(resolved, ResolvedLlmModel)
    assert resolved.base_url == "https://api.deepseek.com"
    assert resolved.context_window == 110_000
    assert resolved.api_key == "legacy-database-credential"
