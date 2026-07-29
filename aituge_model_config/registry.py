from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml


MODEL_MODES = frozenset({"api", "local"})
AI_MODES = frozenset({"public", "private"})
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class LlmModelRegistration:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    credential_ref: str | None = None
    context_window: int = 110_000
    max_tokens: int = 8_000
    temperature: float = 0.1
    enable_thinking: bool = False
    vision_support: bool = False


@dataclass(frozen=True, slots=True)
class EmbeddingModelRegistration:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    dimensions: int
    credential_ref: str | None = None
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RerankerModelRegistration:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    credential_ref: str | None = None
    timeout_seconds: float = 30.0
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class ModelPackRegistration:
    id: str
    display_name: str
    llm: str
    embedding: str
    reranker: str


@dataclass(frozen=True, slots=True)
class ResolvedModelPack:
    id: str
    display_name: str
    llm: LlmModelRegistration
    embedding: EmbeddingModelRegistration
    reranker: RerankerModelRegistration


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    default_pack_id: str
    llms: Mapping[str, LlmModelRegistration]
    embeddings: Mapping[str, EmbeddingModelRegistration]
    rerankers: Mapping[str, RerankerModelRegistration]
    packs: Mapping[str, ModelPackRegistration]

    @classmethod
    def from_directory(cls, directory: str | Path) -> "ModelRegistry":
        root = Path(directory).expanduser().resolve()
        components = _load_yaml(root / "components.yaml")
        default_pack_id = _required_text(components, "default_pack_id", "components.yaml")
        llms = _component_map(
            components.get("llms"),
            kind="llm",
            factory=_llm_registration,
        )
        embeddings = _component_map(
            components.get("embeddings"),
            kind="embedding",
            factory=_embedding_registration,
        )
        rerankers = _component_map(
            components.get("rerankers"),
            kind="reranker",
            factory=_reranker_registration,
        )

        packs: dict[str, ModelPackRegistration] = {}
        pack_dir = root / "packs"
        if not pack_dir.is_dir():
            raise ValueError(f"Model pack directory does not exist: {pack_dir}")
        for path in sorted(pack_dir.glob("*.yaml")):
            data = _load_yaml(path)
            registration = ModelPackRegistration(
                id=_required_text(data, "id", str(path)),
                display_name=_optional_text(data, "display_name")
                or _required_text(data, "id", str(path)),
                llm=_required_text(data, "llm", str(path)),
                embedding=_required_text(data, "embedding", str(path)),
                reranker=_required_text(data, "reranker", str(path)),
            )
            if registration.id in packs:
                raise ValueError(f"Duplicate model pack id: {registration.id}")
            packs[registration.id] = registration

        registry = cls(
            default_pack_id=default_pack_id,
            llms=llms,
            embeddings=embeddings,
            rerankers=rerankers,
            packs=packs,
        )
        for pack_id in packs:
            registry.resolve_pack(pack_id)
        if default_pack_id not in packs:
            raise ValueError(f"Unknown default model pack: {default_pack_id}")
        return registry

    def resolve_pack(self, pack_id: str | None = None) -> ResolvedModelPack:
        resolved_id = (pack_id or self.default_pack_id).strip()
        pack = self.packs.get(resolved_id)
        if pack is None:
            raise ValueError(f"Unknown model pack: {resolved_id}")
        try:
            llm = self.llms[pack.llm]
            embedding = self.embeddings[pack.embedding]
            reranker = self.rerankers[pack.reranker]
        except KeyError as exc:
            raise ValueError(
                f"Model pack '{pack.id}' references unknown component '{exc.args[0]}'."
            ) from exc
        return ResolvedModelPack(
            id=pack.id,
            display_name=pack.display_name,
            llm=llm,
            embedding=embedding,
            reranker=reranker,
        )


class SecretResolver:
    """Resolve deployment credentials without putting secret values in model packs."""

    def __init__(
        self,
        *,
        secret_dir: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> None:
        self.secret_dir = Path(
            secret_dir
            or os.getenv("MODEL_SECRET_DIR", "")
            or DEFAULT_CONFIG_DIR / "secrets"
        ).expanduser()
        self.environment = environment if environment is not None else os.environ
        self.overrides = dict(overrides or {})

    def resolve(self, credential_ref: str | None, *, required: bool) -> str:
        if not credential_ref:
            if required:
                raise ValueError("Model credential_ref is required.")
            return ""
        override = self.overrides.get(credential_ref, "").strip()
        if override:
            return override
        env_name = "MODEL_SECRET_" + re.sub(
            r"[^A-Za-z0-9]+", "_", credential_ref
        ).upper()
        env_value = str(self.environment.get(env_name, "")).strip()
        if env_value:
            return env_value
        secret_path = self.secret_dir / credential_ref
        if secret_path.is_file():
            value = secret_path.read_text("utf-8").strip()
            if value:
                return value
        if required:
            raise ValueError(
                f"Model credential '{credential_ref}' is not configured in "
                f"{env_name} or {secret_path}."
            )
        return ""


@lru_cache(maxsize=8)
def load_model_registry(directory: str = "") -> ModelRegistry:
    configured = directory or os.getenv("MODEL_CONFIG_DIR", "")
    return ModelRegistry.from_directory(configured or DEFAULT_CONFIG_DIR)


def get_active_model_pack(
    *,
    directory: str = "",
    pack_id: str = "",
) -> ResolvedModelPack:
    registry = load_model_registry(directory)
    selected = pack_id or os.getenv("MODEL_PACK_ID", "") or registry.default_pack_id
    return registry.resolve_pack(selected)


def get_model_pack_for_ai_mode(
    mode: str,
    *,
    directory: str = "",
    environment: Mapping[str, str] | None = None,
) -> ResolvedModelPack:
    """Resolve the user-facing public/private mode to a registered package."""

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in AI_MODES:
        raise ValueError("AI mode must be public or private.")
    env = environment if environment is not None else os.environ
    registry = load_model_registry(directory)
    configured_id = str(
        env.get(
            "MODEL_PACK_PUBLIC_ID"
            if normalized_mode == "public"
            else "MODEL_PACK_PRIVATE_ID",
            "",
        )
        or ""
    ).strip()
    pack_id = configured_id or (
        registry.default_pack_id
        if normalized_mode == "public"
        else "api-rerank-similarity"
    )
    return registry.resolve_pack(pack_id)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Model configuration file does not exist: {path}")
    data = yaml.safe_load(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Model configuration must be an object: {path}")
    return data


def _component_map(raw: Any, *, kind: str, factory):
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"At least one {kind} model must be registered.")
    result = {}
    for component_id, values in raw.items():
        if not isinstance(values, dict):
            raise ValueError(f"Model component '{component_id}' must be an object.")
        normalized_id = str(component_id).strip()
        if not normalized_id:
            raise ValueError(f"Empty {kind} model id.")
        result[normalized_id] = factory(normalized_id, values)
    return result


def _llm_registration(component_id: str, values: Mapping[str, Any]):
    return LlmModelRegistration(
        id=component_id,
        mode=_mode(values, component_id),
        provider=_required_text(values, "provider", component_id),
        model=_required_text(values, "model", component_id),
        base_url=_required_text(values, "base_url", component_id),
        credential_ref=_optional_text(values, "credential_ref"),
        context_window=_positive_int(values.get("context_window", 110_000), "context_window"),
        max_tokens=_positive_int(values.get("max_tokens", 8_000), "max_tokens"),
        temperature=float(values.get("temperature", 0.1)),
        enable_thinking=bool(values.get("enable_thinking", False)),
        vision_support=bool(values.get("vision_support", False)),
    )


def _embedding_registration(component_id: str, values: Mapping[str, Any]):
    return EmbeddingModelRegistration(
        id=component_id,
        mode=_mode(values, component_id),
        provider=_required_text(values, "provider", component_id),
        model=_required_text(values, "model", component_id),
        base_url=_required_text(values, "base_url", component_id),
        dimensions=_positive_int(values.get("dimensions"), "dimensions"),
        credential_ref=_optional_text(values, "credential_ref"),
        timeout_seconds=_positive_float(
            values.get("timeout_seconds", 30), "timeout_seconds"
        ),
    )


def _reranker_registration(component_id: str, values: Mapping[str, Any]):
    return RerankerModelRegistration(
        id=component_id,
        mode=_mode(values, component_id),
        provider=_required_text(values, "provider", component_id),
        model=_required_text(values, "model", component_id),
        base_url=_required_text(values, "base_url", component_id),
        credential_ref=_optional_text(values, "credential_ref"),
        timeout_seconds=_positive_float(
            values.get("timeout_seconds", 30), "timeout_seconds"
        ),
        instruction=str(values.get("instruction", "") or "").strip(),
    )


def _mode(values: Mapping[str, Any], source: str) -> str:
    mode = _required_text(values, "mode", source)
    if mode not in MODEL_MODES:
        raise ValueError(f"Unsupported model mode '{mode}' for '{source}'.")
    return mode


def _required_text(values: Mapping[str, Any], key: str, source: str) -> str:
    value = str(values.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing '{key}' in model configuration '{source}'.")
    return value


def _optional_text(values: Mapping[str, Any], key: str) -> str | None:
    value = str(values.get(key, "")).strip()
    return value or None


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Model field '{field}' must be a positive integer.") from exc
    if result <= 0:
        raise ValueError(f"Model field '{field}' must be a positive integer.")
    return result


def _positive_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Model field '{field}' must be positive.") from exc
    if result <= 0:
        raise ValueError(f"Model field '{field}' must be positive.")
    return result
