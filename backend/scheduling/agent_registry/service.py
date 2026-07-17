from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from common.llm.constants import DEFAULT_LLM_MODEL_ID
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .defaults import (
    LEGACY_DEFAULT_SYSTEM_PROMPTS,
    LEGACY_DEFAULT_TOOLS,
    build_default_agent_profiles,
)
from .models import AgentProfileEntity


def _merge_missing(defaults: dict[str, Any], configured: dict[str, Any]) -> dict[str, Any]:
    """Add new default-owned keys without overwriting user configuration."""
    merged = dict(configured)
    for key, default_value in defaults.items():
        current_value = merged.get(key)
        if isinstance(default_value, dict) and isinstance(current_value, dict):
            merged[key] = _merge_missing(default_value, current_value)
        elif key not in merged:
            merged[key] = default_value
    return merged


async def ensure_default_agent_profiles(session: AsyncSession) -> None:
    for profile in build_default_agent_profiles():
        existing = await session.get(AgentProfileEntity, profile.agent_id)
        if existing is None:
            session.add(profile)
        else:
            changed = False
            if not existing.system_prompt and profile.system_prompt:
                existing.system_prompt = profile.system_prompt
                changed = True
            elif existing.system_prompt in LEGACY_DEFAULT_SYSTEM_PROMPTS.get(
                profile.agent_id, set()
            ):
                existing.system_prompt = profile.system_prompt
                changed = True
            if tuple(existing.default_tools) in LEGACY_DEFAULT_TOOLS.get(
                profile.agent_id, set()
            ):
                existing.default_tools_json = profile.default_tools_json
                changed = True
            merged_runtime_config = _merge_missing(
                profile.runtime_config,
                existing.runtime_config,
            )
            if merged_runtime_config != existing.runtime_config:
                existing.runtime_config_json = json.dumps(
                    merged_runtime_config,
                    ensure_ascii=True,
                )
                changed = True
            if not changed:
                continue
            existing.updated_at = datetime.utcnow()
            session.add(existing)
    await session.commit()


async def list_agent_profiles(
    session: AsyncSession,
    enabled_only: bool = True,
) -> list[AgentProfileEntity]:
    statement = select(AgentProfileEntity).order_by(AgentProfileEntity.created_at)
    if enabled_only:
        statement = statement.where(AgentProfileEntity.enabled == True)  # noqa: E712
    result = await session.exec(statement)
    return list(result.all())


async def get_agent_profile(
    session: AsyncSession,
    agent_id: str,
) -> AgentProfileEntity | None:
    return await session.get(AgentProfileEntity, agent_id)


async def upsert_agent_profile(
    session: AsyncSession,
    *,
    agent_id: str,
    name: str,
    description: str = "",
    agent_type: str = "single",
    model_id: str = DEFAULT_LLM_MODEL_ID,
    system_prompt: str = "",
    default_tools: list[str] | None = None,
    default_datasets: list[str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> AgentProfileEntity:
    profile = await session.get(AgentProfileEntity, agent_id)
    now = datetime.utcnow()
    values = {
        "name": name,
        "description": description,
        "agent_type": agent_type,
        "model_id": model_id,
        "system_prompt": system_prompt,
        "default_tools_json": json.dumps(default_tools or [], ensure_ascii=True),
        "default_datasets_json": json.dumps(default_datasets or [], ensure_ascii=True),
        "runtime_config_json": json.dumps(runtime_config or {}, ensure_ascii=True),
        "enabled": enabled,
        "updated_at": now,
    }
    if profile is None:
        profile = AgentProfileEntity(agent_id=agent_id, created_at=now, **values)
        session.add(profile)
    else:
        for key, value in values.items():
            setattr(profile, key, value)
        session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile
