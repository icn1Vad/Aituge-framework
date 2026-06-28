"""Build task skill bundles from task-level skill names."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .bundle import SkillBundle
from .catalog import get_skill


def build_skill_bundle(
    *,
    primary_skill: str | None = None,
    candidate_skills: Iterable[str] | None = None,
    skills_dir: str | Path | None = None,
) -> SkillBundle:
    primary = get_skill(primary_skill, skills_dir) if primary_skill else None
    candidates = [
        get_skill(name, skills_dir)
        for name in (candidate_skills or [])
        if name
    ]
    return SkillBundle.from_skills(primary=primary, candidates=candidates)
