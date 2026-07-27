"""DB-backed helpers for building task-owned tool bundles."""

from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.system_constants import DEFAULT_TENANT_ID

from .models import ToolConfigEntity


async def get_enabled_tool_configs(
    session: AsyncSession,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[ToolConfigEntity]:
    statement = select(ToolConfigEntity).where(
        ToolConfigEntity.tenant_id == tenant_id,
        ToolConfigEntity.enabled == True,  # noqa: E712
    )
    try:
        result = await session.exec(statement)
    except OperationalError as exc:
        if "tuge_tool_config" in str(exc):
            logger.warning("tuge_tool_config table does not exist; no DB tools loaded.")
            return []
        raise
    return list(result.all())


async def get_tool_configs_by_names(
    session: AsyncSession,
    tool_names: list[str],
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[ToolConfigEntity]:
    names = [name for name in dict.fromkeys(tool_names) if name]
    if not names:
        return []
    statement = select(ToolConfigEntity).where(
        ToolConfigEntity.tenant_id == tenant_id,
        ToolConfigEntity.tool_name.in_(names),
    )
    try:
        result = await session.exec(statement)
    except OperationalError as exc:
        if "tuge_tool_config" in str(exc):
            logger.warning("tuge_tool_config table does not exist; no DB tools loaded.")
            return []
        raise
    tenant_configs = list(result.all())
    configs = [item for item in tenant_configs if item.enabled]
    found_names = {item.tool_name for item in tenant_configs}
    missing = [name for name in names if name not in found_names]
    if missing and tenant_id != DEFAULT_TENANT_ID:
        fallback_statement = select(ToolConfigEntity).where(
            ToolConfigEntity.tenant_id == DEFAULT_TENANT_ID,
            ToolConfigEntity.enabled == True,  # noqa: E712
            ToolConfigEntity.tool_name.in_(missing),
        )
        fallback = await session.exec(fallback_statement)
        configs.extend(fallback.all())
    return configs
