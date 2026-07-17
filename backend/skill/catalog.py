"""Read-only catalog for available task skills."""

from __future__ import annotations

from pathlib import Path

from .loader import DEFAULT_SKILLS_DIR, SKILL_FILE_NAME, load_skill
from .models import Skill, SkillSummary


_EXTERNAL_SKILL_ROOTS: list[Path] = []


def skill_roots() -> list[Path]:
    return [DEFAULT_SKILLS_DIR, *_EXTERNAL_SKILL_ROOTS]


def register_skill_root(skills_dir: str | Path) -> Path:
    """Add one trusted external skill directory after checking name conflicts."""

    root = Path(skills_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Skill root does not exist or is not a directory: {root}")
    if root == DEFAULT_SKILLS_DIR.resolve() or root in _EXTERNAL_SKILL_ROOTS:
        return root

    existing = {
        skill.name: skill.path
        for path in _iter_roots(skill_roots())
        for skill in [load_skill(path)]
    }
    incoming: dict[str, Path] = {}
    for path in _iter_root(root):
        skill = load_skill(path)
        if skill.name in incoming:
            raise ValueError(
                f"Skill name '{skill.name}' is duplicated inside external root: "
                f"{incoming[skill.name]}, {skill.path}"
            )
        if skill.name in existing:
            raise ValueError(
                f"Skill name '{skill.name}' conflicts with existing skill: "
                f"{existing[skill.name]}"
            )
        incoming[skill.name] = skill.path

    if not incoming:
        raise ValueError(f"Skill root contains no {SKILL_FILE_NAME} files: {root}")
    _EXTERNAL_SKILL_ROOTS.append(root)
    return root


def _iter_root(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob(SKILL_FILE_NAME)
        if "__pycache__" not in path.parts
    )


def _iter_roots(roots: list[Path]) -> list[Path]:
    return [path for root in roots for path in _iter_root(root)]


def iter_skill_files(skills_dir: str | Path | None = None) -> list[Path]:
    if skills_dir is not None:
        return _iter_root(Path(skills_dir))
    return _iter_roots(skill_roots())


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
