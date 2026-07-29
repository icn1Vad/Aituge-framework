from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from aituge_model_config import ModelRuntimeProvider, get_model_pack_for_ai_mode

from proof.errors import ProofError


MODEL_PACK_ID_HEADER = "X-Model-Pack-ID"
AI_MODE_HEADER = "X-AI-Mode"

_current_model_pack_id: ContextVar[str] = ContextVar(
    "proof_current_model_pack_id",
    default="",
)


def resolve_model_pack_id(settings, value: str | None = None) -> str:
    requested = str(value or "").strip()
    configured = (
        requested
        or str(getattr(settings, "model_pack_id", "") or "").strip()
        or os.getenv("MODEL_PACK_ID", "").strip()
    )
    if not configured:
        return ""
    try:
        provider = ModelRuntimeProvider.from_environment(
            directory=str(getattr(settings, "model_config_dir", "") or "").strip(),
            pack_id=configured,
            secret_dir=(
                str(getattr(settings, "model_secret_dir", "") or "").strip()
                or None
            ),
        )
    except ValueError as exc:
        raise ProofError(
            "invalid_model_pack_id",
            f"Unknown model pack '{configured}'.",
            status_code=400,
        ) from exc
    return provider.active_pack.id


def resolve_ai_mode_model_pack_id(settings, value: str | None) -> str | None:
    mode = str(value or "").strip().lower()
    if not mode:
        return None
    try:
        return get_model_pack_for_ai_mode(
            mode,
            directory=str(getattr(settings, "model_config_dir", "") or "").strip(),
        ).id
    except ValueError as exc:
        raise ProofError(
            "invalid_ai_mode",
            "X-AI-Mode must be public or private and map to a registered model pack.",
            status_code=400,
        ) from exc


def current_model_pack_id() -> str:
    return _current_model_pack_id.get()


@contextmanager
def model_pack_scope(model_pack_id: str) -> Iterator[str]:
    token: Token[str] = _current_model_pack_id.set(str(model_pack_id or "").strip())
    try:
        yield _current_model_pack_id.get()
    finally:
        _current_model_pack_id.reset(token)
