"""Tool adapters for progressively reading task skills."""

from __future__ import annotations

from collections.abc import Iterable

from llama_index.core.tools.function_tool import FunctionTool

from .models import Skill


def create_read_skill_tool_for_skills(skills: Iterable[Skill]) -> FunctionTool | None:
    """Create a run-scoped tool that reads full selected auxiliary skills."""

    available = {skill.name: skill for skill in skills}
    if not available:
        return None

    names = ", ".join(sorted(available))

    async def read_skill(skill_name: str) -> str:
        skill = available.get(skill_name)
        if not skill:
            return f"Skill '{skill_name}' is not available in this run. Available skills: {names}."
        return "\n".join(
            [
                f"# Skill: {skill.name}",
                f"Description: {skill.description}",
                f"Path: {skill.path}",
                "",
                skill.content.strip(),
            ]
        )

    return FunctionTool.from_defaults(
        async_fn=read_skill,
        name="ReadSkill",
        description=(
            "Read the full instructions for one auxiliary skill available in this run. "
            "Call this for one skill at a time; do not call multiple ReadSkill tools in parallel. "
            f"Use this before applying an auxiliary skill. Available skill_name values: {names}."
        ),
        return_direct=False,
    )
