"""DB-backed helpers for building task-owned tool bundles."""

from __future__ import annotations

from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.system_constants import DEFAULT_TENANT_ID
from tool.bundle import ToolBundle

from .models import ToolConfigEntity
from .registry import ToolList
from .tool_list import get_default_tool_list


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
        ToolConfigEntity.enabled == True,  # noqa: E712
        ToolConfigEntity.tool_name.in_(names),
    )
    try:
        result = await session.exec(statement)
    except OperationalError as exc:
        if "tuge_tool_config" in str(exc):
            logger.warning("tuge_tool_config table does not exist; no DB tools loaded.")
            return []
        raise
    return list(result.all())


async def create_enabled_tool_bundle(
    session: AsyncSession,
    tenant_id: str = DEFAULT_TENANT_ID,
    tool_list: ToolList | None = None,
) -> ToolBundle:
    registry = tool_list or get_default_tool_list()
    configs = await get_enabled_tool_configs(session=session, tenant_id=tenant_id)
    bundles = []
    for entity in configs:
        try:
            bundles.append(registry.create_bundle(entity.to_provider_config()))
        except Exception:
            logger.exception(
                "Failed to create tool bundle for {}/{}",
                entity.tool_name,
                entity.provider,
            )
            raise
    return ToolBundle.combine(bundles)
