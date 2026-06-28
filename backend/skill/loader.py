"""Filesystem loader for task skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Skill, SkillMetadata


SKILL_FILE_NAME = "SKILL.md"
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_scalar(value: str) -> Any:
    value = _strip_quotes(value.strip())
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",")
        return [_strip_quotes(item.strip()) for item in items if item.strip()]
    return value


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content

    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content

    raw_frontmatter = content[4:end]
    body = content[end + len("\n---\n") :]
    metadata: dict[str, Any] = {}
    for line in raw_frontmatter.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = _parse_scalar(value)
    return metadata, body.lstrip()


def _resolve_skill_path(name_or_path: str | Path, skills_dir: Path) -> Path:
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        return candidate / SKILL_FILE_NAME if candidate.is_dir() else candidate

    direct = skills_dir / candidate
    if direct.is_dir():
        return direct / SKILL_FILE_NAME
    if direct.name == SKILL_FILE_NAME or direct.suffix == ".md":
        return direct
    return direct / SKILL_FILE_NAME


def load_skill(
    name_or_path: str | Path,
    *,
    skills_dir: str | Path | None = None,
) -> Skill:
    root = Path(skills_dir) if skills_dir is not None else DEFAULT_SKILLS_DIR
    skill_path = _resolve_skill_path(name_or_path, root)
    content = skill_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)

    name = str(frontmatter.get("name") or skill_path.parent.name).strip()
    if not name:
        raise ValueError(f"Skill at {skill_path} has no name.")

    description = str(frontmatter.get("description") or "").strip()
    tags = frontmatter.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    return Skill(
        metadata=SkillMetadata(
            name=name,
            description=description,
            version=str(frontmatter["version"]) if "version" in frontmatter else None,
            tags=[str(tag) for tag in tags],
            raw=frontmatter,
        ),
        content=body,
        path=skill_path,
    )
