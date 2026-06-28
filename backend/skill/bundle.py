"""Task-scoped skill bundle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .models import Skill


@dataclass(slots=True)
class SkillBundle:
    """Skills selected by the task layer for one agent run."""

    primary: Skill | None = None
    candidates: list[Skill] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "SkillBundle":
        return cls()

    @classmethod
    def from_skills(
        cls,
        *,
        primary: Skill | None = None,
        candidates: Iterable[Skill] | None = None,
    ) -> "SkillBundle":
        return cls(primary=primary, candidates=list(candidates or []))

    @property
    def skills(self) -> list[Skill]:
        return ([self.primary] if self.primary else []) + list(self.candidates)

    def render_prompt(self) -> str:
        sections: list[str] = []
        if self.primary:
            sections.append(
                "\n".join(
                    [
                        "# Primary Task Skill",
                        f"Name: {self.primary.name}",
                        f"Description: {self.primary.description}",
                        "",
                        self.primary.content.strip(),
                    ]
                ).strip()
            )

        if self.candidates:
            lines = [
                "# Available Auxiliary Skills",
                "These skills are allowed for this task. Use them only when they are relevant.",
                "If ReadSkill is available, call ReadSkill with skill_name before applying an auxiliary skill.",
                "Call ReadSkill for one skill at a time; do not call multiple ReadSkill tools in parallel.",
            ]
            for skill in self.candidates:
                description = f": {skill.description}" if skill.description else ""
                lines.append(f"- {skill.name}{description}")
            sections.append("\n".join(lines))

        return "\n\n".join(section for section in sections if section.strip())
