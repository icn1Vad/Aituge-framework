"""DB-backed registry helpers for skill packages."""

from __future__ import annotations

import json
from datetime import datetime

from common.system_constants import DEFAULT_TENANT_ID
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .package_models import SkillPackageEntity


DEFAULT_SKILL_PACKAGES = [
    {
        "package_name": "pipeline-demo-package",
        "display_name": "Pipeline Demo Package",
        "description": "Business-neutral structured output package for Pipeline runtime validation.",
        "tags": ["pipeline", "task-manager", "test"],
        "primary_skill": "pipeline-demo",
        "auxiliary_skills": [],
    },
    {
        "package_name": "general-package",
        "display_name": "General Task Package",
        "description": "General task planning, review, debugging, and concise answer style.",
        "tags": ["general", "task"],
        "primary_skill": "task-style",
        "auxiliary_skills": [
            "implementation-plan",
            "debugging-checklist",
            "review-style",
            "concise-summary",
        ],
    },
    {
        "package_name": "report-package",
        "display_name": "Report Package",
        "description": "Complete evidence-grounded report writing package.",
        "tags": ["report", "analysis"],
        "primary_skill": "report-generator",
        "auxiliary_skills": [
            "report-context-scope",
            "report-executive-summary",
            "report-analysis-findings",
            "report-quantitative-calculation",
            "report-chart-figure",
            "report-code-verification",
            "report-risk-actions",
        ],
    },
    {
        "package_name": "media-script-generate-package",
        "display_name": "Media Script Generate Package",
        "description": "Generate structured short-video scripts for reusable TaskManager media tasks.",
        "tags": ["media", "script", "task-manager"],
        "primary_skill": "media-script-generator",
        "auxiliary_skills": ["media-script-selector"],
    },
    {
        "package_name": "media-script-research-package",
        "display_name": "Media Script Research Package",
        "description": "Build a source-backed research bundle for one media script task.",
        "tags": ["media", "script", "research", "pipeline"],
        "primary_skill": "media-script-research",
        "auxiliary_skills": [],
    },
    {
        "package_name": "media-script-writer-package",
        "display_name": "Media Script Writer Package",
        "description": "Write a structured short-video script from verified Pipeline artifacts.",
        "tags": ["media", "script", "writer", "pipeline"],
        "primary_skill": "media-script-writer",
        "auxiliary_skills": [],
    },
    {
        "package_name": "media-storyboard-package",
        "display_name": "Media Storyboard Package",
        "description": "Generate executable shots from a structured script draft.",
        "tags": ["media", "script", "storyboard", "pipeline"],
        "primary_skill": "media-storyboard",
        "auxiliary_skills": [],
    },
    {
        "package_name": "media-script-review-package",
        "display_name": "Media Script Review Package",
        "description": "Review script and storyboard artifacts against evidence and deterministic checks.",
        "tags": ["media", "script", "review", "pipeline"],
        "primary_skill": "media-script-review",
        "auxiliary_skills": [],
    },
    {
        "package_name": "media-script-select-package",
        "display_name": "Media Script Select Package",
        "description": "Select and explain the best script candidate for reusable TaskManager media tasks.",
        "tags": ["media", "script", "selection", "task-manager"],
        "primary_skill": "media-script-selector",
        "auxiliary_skills": ["media-script-generator"],
    },
    {
        "package_name": "table-audit-package",
        "display_name": "Table Audit Package",
        "description": "Audit table rows one by one and return structured item-level decisions.",
        "tags": ["table", "audit", "task-manager"],
        "primary_skill": "table-audit",
        "auxiliary_skills": [],
    },
    {
        "package_name": "ai-search-package",
        "display_name": "AI Search Package",
        "description": "Source-backed conversational search with structured result cards.",
        "tags": ["search", "web", "task-manager"],
        "primary_skill": "ai-search",
        "auxiliary_skills": [],
    },
    {
        "package_name": "media-topic-search-package",
        "display_name": "Media Topic Search Package",
        "description": "Freshness-first source search and topic aggregation for new-media planning.",
        "tags": ["media", "topic", "search", "task-manager"],
        "primary_skill": "media-topic-search",
        "auxiliary_skills": [],
    },
    {
        "package_name": "douyin-account-report-package",
        "display_name": "Douyin Account Report Package",
        "description": "Fact-grounded Douyin account operations reporting across all available data or a requested range.",
        "tags": ["douyin", "analytics", "report", "task-manager"],
        "primary_skill": "douyin-account-report",
        "auxiliary_skills": [],
    },
]


async def ensure_default_skill_packages(
    session: AsyncSession,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    for definition in DEFAULT_SKILL_PACKAGES:
        statement = select(SkillPackageEntity).where(
            SkillPackageEntity.tenant_id == tenant_id,
            SkillPackageEntity.package_name == definition["package_name"],
        )
        result = await session.exec(statement)
        if result.first() is not None:
            continue
        session.add(
            SkillPackageEntity(
                tenant_id=tenant_id,
                package_name=definition["package_name"],
                display_name=definition["display_name"],
                description=definition["description"],
                tags_json=json.dumps(definition["tags"], ensure_ascii=True),
                primary_skill=definition["primary_skill"],
                auxiliary_skills_json=json.dumps(
                    definition["auxiliary_skills"],
                    ensure_ascii=True,
                ),
            )
        )
    await session.commit()


async def list_skill_packages(
    session: AsyncSession,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    enabled_only: bool = True,
) -> list[SkillPackageEntity]:
    statement = select(SkillPackageEntity).where(
        SkillPackageEntity.tenant_id == tenant_id,
    )
    if enabled_only:
        statement = statement.where(SkillPackageEntity.enabled == True)  # noqa: E712
    statement = statement.order_by(SkillPackageEntity.created_at)
    result = await session.exec(statement)
    return list(result.all())


async def get_skill_packages_by_names(
    session: AsyncSession,
    package_names: list[str],
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[SkillPackageEntity]:
    names = [name for name in dict.fromkeys(package_names) if name]
    if not names:
        return []
    statement = select(SkillPackageEntity).where(
        SkillPackageEntity.tenant_id == tenant_id,
        SkillPackageEntity.package_name.in_(names),
    )
    result = await session.exec(statement)
    found = {item.package_name: item for item in result.all()}
    missing = [name for name in names if name not in found]
    if missing:
        raise ValueError(f"Skill package not found: {', '.join(missing)}")
    return [found[name] for name in names]


async def upsert_skill_package(
    session: AsyncSession,
    *,
    package_name: str,
    display_name: str = "",
    description: str = "",
    tags: list[str] | None = None,
    primary_skill: str,
    auxiliary_skills: list[str] | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    enabled: bool = True,
) -> SkillPackageEntity:
    statement = select(SkillPackageEntity).where(
        SkillPackageEntity.tenant_id == tenant_id,
        SkillPackageEntity.package_name == package_name,
    )
    result = await session.exec(statement)
    package = result.first()
    now = datetime.utcnow()
    values = {
        "display_name": display_name,
        "description": description,
        "tags_json": json.dumps(tags or [], ensure_ascii=True),
        "primary_skill": primary_skill,
        "auxiliary_skills_json": json.dumps(auxiliary_skills or [], ensure_ascii=True),
        "enabled": enabled,
        "updated_at": now,
    }
    if package is None:
        package = SkillPackageEntity(
            tenant_id=tenant_id,
            package_name=package_name,
            created_at=now,
            **values,
        )
        session.add(package)
    else:
        for key, value in values.items():
            setattr(package, key, value)
        session.add(package)
    await session.commit()
    await session.refresh(package)
    return package
