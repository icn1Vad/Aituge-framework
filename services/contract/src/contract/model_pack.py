from __future__ import annotations

from aituge_model.config import ModelRuntimeProvider

from contract.errors import ContractError


def resolve_model_pack_id(value: str | None = None) -> str:
    requested = str(value or "").strip()
    try:
        provider = ModelRuntimeProvider.from_environment(pack_id=requested)
    except ValueError as exc:
        raise ContractError(
            "INVALID_MODEL_PACK",
            f"Unknown model pack '{requested}'.",
            status_code=400,
            user_action_required=True,
        ) from exc
    return provider.active_pack.id
