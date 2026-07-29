"""Unified read-only catalog for visible tool and skill capabilities."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from skill import ensure_default_skill_packages, list_skill_packages
from tool.registry import (
    ToolConfigEntity,
    get_default_tool_list,
    remove_retired_framework_tool_configs,
)


def _tool_tags(tool_name: str, provider: str) -> list[str]:
    values = ["tool"]
    for item in [tool_name, provider]:
        values.extend(part for part in item.replace("-", "_").split("_") if part)
    return list(dict.fromkeys(values))


async def list_capabilities(
    session: AsyncSession,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    """Return a unified display catalog without exposing executable secrets."""

    await remove_retired_framework_tool_configs(session, tenant_id=tenant_id)
    await ensure_default_skill_packages(session, tenant_id=tenant_id)
    tool_configs = await _list_tool_configs(session, tenant_id)
    tool_config_by_key = {
        (config.tool_name, config.provider): config
        for config in tool_configs
    }

    capabilities: list[dict[str, Any]] = []
    for definition in get_default_tool_list().list():
        config = tool_config_by_key.get((definition.tool_name, definition.provider))
        capabilities.append(
            {
                "capability_type": "tool",
                "name": definition.tool_name,
                "provider": definition.provider,
                "display_name": definition.display_name,
                "description": definition.description,
                "tags": _tool_tags(definition.tool_name, definition.provider),
                "enabled": config.enabled if config else True,
                "configured": config is not None,
                "llm_names": list(definition.llm_tool_names),
                "inner": {
                    "kind": "tool_config",
                    "tool_name": definition.tool_name,
                    "provider": definition.provider,
                },
            }
        )

    skill_packages = await list_skill_packages(
        session,
        tenant_id=tenant_id,
        enabled_only=False,
    )
    for package in skill_packages:
        capabilities.append(
            {
                "capability_type": "skill_package",
                "name": package.package_name,
                "provider": None,
                "display_name": package.display_name,
                "description": package.description,
                "tags": package.tags,
                "enabled": package.enabled,
                "configured": True,
                "primary": package.primary_skill,
                "auxiliary": package.auxiliary_skills,
                "inner": {
                    "kind": "skill_package",
                    "package_name": package.package_name,
                },
            }
        )

    return sorted(
        capabilities,
        key=lambda item: (
            str(item["capability_type"]),
            str(item["name"]),
            str(item.get("provider") or ""),
        ),
    )


async def _list_tool_configs(
    session: AsyncSession,
    tenant_id: str,
) -> list[ToolConfigEntity]:
    result = await session.exec(
        select(ToolConfigEntity).where(ToolConfigEntity.tenant_id == tenant_id)
    )
    return list(result.all())


def create_capability_router() -> APIRouter:
    router = APIRouter(prefix="/registry", tags=["registry"])

    @router.get("/capabilities")
    async def capabilities():
        async with create_db_session() as session:
            items = await list_capabilities(session)
        return {"capabilities": items}

    return router
