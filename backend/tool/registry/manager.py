"""Runtime assembly for task-selected non-RAG tools."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from loguru import logger
from sqlmodel.ext.asyncio.session import AsyncSession

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from tool.bundle import ToolBundle
from tool.artifacts import ArtifactPublisher

from .config import ToolProviderConfig
from .models import ToolConfigEntity
from .registry import ToolList
from .service import get_enabled_tool_configs, get_tool_configs_by_names
from .tool_list import get_default_tool_list


class ToolManager:
    """Resolve selected non-RAG tool names into a ToolBundle."""

    def __init__(
        self,
        *,
        local_python_work_dir: Path,
        artifact_publisher: ArtifactPublisher | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        tool_list: ToolList | None = None,
    ) -> None:
        self.local_python_work_dir = local_python_work_dir
        self.artifact_publisher = artifact_publisher
        self.tenant_id = tenant_id
        self.tool_list = tool_list or get_default_tool_list()

    async def create_bundle(
        self,
        tool_names: list[str],
        session: Optional[AsyncSession] = None,
    ) -> ToolBundle:
        names = _dedupe(tool_names)
        if not names:
            return ToolBundle.empty()

        bundles = []
        if "code_interpreter" in names or "local_python" in names:
            bundles.append(self._create_local_python_bundle())

        db_names = [
            name
            for name in names
            if name not in {"code_interpreter", "local_python", "enabled_db_tools"}
        ]
        load_all_db_tools = "enabled_db_tools" in names
        if load_all_db_tools or db_names:
            if session is None:
                async with create_db_session() as owned_session:
                    bundles.extend(
                        await self._create_db_bundles(
                            owned_session,
                            load_all=load_all_db_tools,
                            tool_names=db_names,
                        )
                    )
            else:
                bundles.extend(
                    await self._create_db_bundles(
                        session,
                        load_all=load_all_db_tools,
                        tool_names=db_names,
                    )
                )

        return ToolBundle.combine(bundles)

    def _create_local_python_bundle(self) -> ToolBundle:
        return self.tool_list.create_bundle(
            ToolProviderConfig(
                tool_name="code_interpreter",
                provider="local_python",
                config={
                    "timeout_seconds": 20,
                    "max_output_chars": 50_000,
                    "work_dir": self.local_python_work_dir,
                    "artifact_publisher": self.artifact_publisher,
                    "keep_work_dir": True,
                    "cleanup_run_dir": True,
                },
            )
        )

    async def _create_db_bundles(
        self,
        session: AsyncSession,
        *,
        load_all: bool,
        tool_names: list[str],
    ) -> list[ToolBundle]:
        configs = []
        if load_all:
            configs.extend(
                await get_enabled_tool_configs(
                    session=session,
                    tenant_id=self.tenant_id,
                )
            )
        if tool_names:
            configs.extend(
                await get_tool_configs_by_names(
                    session=session,
                    tool_names=tool_names,
                    tenant_id=self.tenant_id,
                )
            )
        return self._create_bundles_from_configs(configs)

    def _create_bundles_from_configs(
        self,
        configs: list[ToolConfigEntity],
    ) -> list[ToolBundle]:
        bundles = []
        seen: set[tuple[str, str]] = set()
        for entity in configs:
            key = (entity.tool_name, entity.provider)
            if key in seen:
                continue
            seen.add(key)
            try:
                bundles.append(self.tool_list.create_bundle(entity.to_provider_config()))
            except KeyError:
                logger.warning(
                    "Configured tool provider {}/{} is not registered; skipping.",
                    entity.tool_name,
                    entity.provider,
                )
            except Exception:
                logger.exception(
                    "Failed to create tool bundle for {}/{}",
                    entity.tool_name,
                    entity.provider,
                )
                raise
        return bundles


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    values = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            values.append(item)
    return values
