"""DB-backed registry helpers for skill packages."""

from __future__ import annotations

import json
from datetime import datetime

from common.system_constants import DEFAULT_TENANT_ID
from sqlalchemy import delete
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
        "package_name": "task-memory-compression-package",
        "display_name": "Task Memory Compression Package",
        "description": "Compress confirmed conversation facts into durable task memory.",
        "tags": ["memory", "task-manager"],
        "primary_skill": "task-memory-compression",
        "auxiliary_skills": [],
    },
]


# Framework-owned packages removed from the current simple-chat product surface.
# Startup deletes only these known legacy names, not tenant-created packages.
RETIRED_DEFAULT_SKILL_PACKAGE_NAMES = {
    "general-package",
    "report-package",
    "media-script-generate-package",
    "main-agent-orchestration-package",
    "media-script-change-proposal-package",
    "media-script-research-package",
    "media-script-review-package",
    "media-history-viral-topic-variants-package",
    "media-industry-calendar-topic-copy-package",
    "media-calendar-official-date-lookup-package",
    "douyin-content-analysis-package",
    "media-script-chat-package",
    "media-script-main-agent-package",
    "media-writer-consult-package",
    "media-writer-delegate-package",
    "media-storyboard-consult-package",
    "media-storyboard-delegate-package",
    "media-script-memory-compression-package",
    "media-script-writer-package",
    "media-storyboard-package",
    "media-script-select-package",
    "media-topic-search-package",
    "douyin-account-report-package",
    "smart-fill-project-package",
    "smart-fill-company-package",
    "smart-fill-financial-package",
    "smart-fill-risk-package",
    "smart-fill-analysis-package",
}


async def ensure_default_skill_packages(
    session: AsyncSession,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    await session.exec(
        delete(SkillPackageEntity).where(
            SkillPackageEntity.tenant_id == tenant_id,
            SkillPackageEntity.package_name.in_(
                RETIRED_DEFAULT_SKILL_PACKAGE_NAMES
            ),
        )
    )
    for definition in DEFAULT_SKILL_PACKAGES:
        statement = select(SkillPackageEntity).where(
            SkillPackageEntity.tenant_id == tenant_id,
            SkillPackageEntity.package_name == definition["package_name"],
        )
        result = await session.exec(statement)
        existing = result.first()
        if existing is not None:
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
    if missing and tenant_id != DEFAULT_TENANT_ID:
        fallback = await session.exec(
            select(SkillPackageEntity).where(
                SkillPackageEntity.tenant_id == DEFAULT_TENANT_ID,
                SkillPackageEntity.package_name.in_(missing),
            )
        )
        found.update({item.package_name: item for item in fallback.all()})
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
