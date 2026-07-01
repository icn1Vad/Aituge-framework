"""Runtime assembly for task-selected skill packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from llama_index.core.tools.function_tool import FunctionTool
from sqlmodel.ext.asyncio.session import AsyncSession

from .catalog import get_skill
from .models import Skill
from .package_models import SkillPackageEntity
from .package_service import ensure_default_skill_packages, get_skill_packages_by_names
from .tool import create_read_skill_tool_for_skills


@dataclass(slots=True)
class SkillContext:
    tools: list[FunctionTool] = field(default_factory=list)
    task_prompt: str = ""
    skills: dict = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "SkillContext":
        return cls()


class SkillManager:
    """Resolve selected skill package names into prompt/tool context."""

    def __init__(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.tenant_id = tenant_id

    async def create_context(
        self,
        package_name: str | None,
        session: Optional[AsyncSession] = None,
    ) -> SkillContext:
        name = (package_name or "").strip()
        if not name:
            return SkillContext.empty()

        if session is None:
            async with create_db_session() as owned_session:
                return await self._create_context_with_session(owned_session, name)
        return await self._create_context_with_session(session, name)

    async def _create_context_with_session(
        self,
        session: AsyncSession,
        package_name: str,
    ) -> SkillContext:
        await ensure_default_skill_packages(session, tenant_id=self.tenant_id)
        packages = await get_skill_packages_by_names(
            session,
            [package_name],
            tenant_id=self.tenant_id,
        )
        package = packages[0]
        if not package.enabled:
            return SkillContext.empty()

        loaded = _load_package(package)
        auxiliary_skills = _dedupe_skills(loaded["auxiliary"])
        read_skill_tool = create_read_skill_tool_for_skills(auxiliary_skills)

        return SkillContext(
            tools=[read_skill_tool] if read_skill_tool else [],
            task_prompt=_render_package_prompt(loaded),
            skills={"active_package": _package_summary(loaded)},
        )


def _load_package(package: SkillPackageEntity) -> dict:
    primary = get_skill(package.primary_skill)
    auxiliary = [get_skill(name) for name in package.auxiliary_skills]
    return {
        "entity": package,
        "primary": primary,
        "auxiliary": auxiliary,
    }


def _render_package_prompt(item: dict) -> str:
    package = item["entity"]
    primary = item["primary"]
    auxiliary = item["auxiliary"]
    lines = [
        "# Active Skill Package",
        f"Package: {package.package_name}",
        f"Display Name: {package.display_name or package.package_name}",
        f"Description: {package.description}",
        "",
        "# Primary Task Skill",
        f"Name: {primary.name}",
        f"Description: {primary.description}",
        "",
        primary.content.strip(),
    ]
    if auxiliary:
        lines.extend(
            [
                "",
                "# Available Auxiliary Skills",
                "These skills are part of the active package. Use them only when they are relevant.",
                "If ReadSkill is available, call ReadSkill with skill_name before applying an auxiliary skill.",
                "Call ReadSkill for one skill at a time; do not call multiple ReadSkill tools in parallel.",
            ]
        )
        for skill in auxiliary:
            description = f": {skill.description}" if skill.description else ""
            lines.append(f"- {skill.name}{description}")
    return "\n".join(line for line in lines if line is not None).strip()


def _package_summary(item: dict) -> dict:
    package = item["entity"]
    return {
        "package_name": package.package_name,
        "display_name": package.display_name,
        "description": package.description,
        "tags": package.tags,
        "primary": _skill_summary(item["primary"]),
        "auxiliary_index": [_skill_summary(skill) for skill in item["auxiliary"]],
    }


def _skill_summary(skill: Skill) -> dict:
    return {
        "name": skill.name,
        "description": skill.description,
        "tags": skill.metadata.tags,
        "path": str(skill.path),
    }


def _dedupe_skills(skills) -> list[Skill]:
    seen = set()
    values = []
    for skill in skills:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        values.append(skill)
    return values
