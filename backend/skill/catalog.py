"""Read-only catalog for available task skills."""

from __future__ import annotations

from pathlib import Path

from .loader import DEFAULT_SKILLS_DIR, SKILL_FILE_NAME, load_skill
from .models import Skill, SkillSummary


def iter_skill_files(skills_dir: str | Path | None = None) -> list[Path]:
    root = Path(skills_dir) if skills_dir is not None else DEFAULT_SKILLS_DIR
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob(SKILL_FILE_NAME)
        if "__pycache__" not in path.parts
    )


def list_skills(skills_dir: str | Path | None = None) -> list[SkillSummary]:
    summaries: list[SkillSummary] = []
    for path in iter_skill_files(skills_dir):
        skill = load_skill(path)
        summaries.append(
            SkillSummary(
                name=skill.name,
                description=skill.description,
                path=skill.path,
                tags=list(skill.metadata.tags),
            )
        )
    return summaries


def get_skill(name: str, skills_dir: str | Path | None = None) -> Skill:
    matches: list[Skill] = []
    for path in iter_skill_files(skills_dir):
        skill = load_skill(path)
        if skill.name == name or skill.path.parent.name == name:
            matches.append(skill)

    if not matches:
        available = ", ".join(summary.name for summary in list_skills(skills_dir))
        suffix = f" Available skills: {available}." if available else ""
        raise ValueError(f"Skill '{name}' not found.{suffix}")
    if len(matches) > 1:
        paths = ", ".join(str(skill.path) for skill in matches)
        raise ValueError(f"Skill name '{name}' is ambiguous: {paths}")
    return matches[0]
