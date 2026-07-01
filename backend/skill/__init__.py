"""Task-scoped skill loading and prompt bundling."""

from .catalog import get_skill, list_skills
from .loader import load_skill
from .manager import SkillContext, SkillManager
from .models import Skill, SkillMetadata, SkillSummary
from .package_models import SkillPackageEntity
from .package_service import (
    ensure_default_skill_packages,
    get_skill_packages_by_names,
    list_skill_packages,
    upsert_skill_package,
)
from .tool import create_read_skill_tool_for_skills

__all__ = [
    "Skill",
    "SkillContext",
    "SkillManager",
    "SkillMetadata",
    "SkillPackageEntity",
    "SkillSummary",
    "create_read_skill_tool_for_skills",
    "ensure_default_skill_packages",
    "get_skill_packages_by_names",
    "get_skill",
    "list_skill_packages",
    "list_skills",
    "load_skill",
    "upsert_skill_package",
]
