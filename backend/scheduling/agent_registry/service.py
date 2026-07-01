from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .defaults import build_default_agent_profiles
from .models import AgentProfileEntity


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
    model_id: str = "deepseek-v4-pro",
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
