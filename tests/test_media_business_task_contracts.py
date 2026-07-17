from __future__ import annotations

import pytest

from skill.package_service import DEFAULT_SKILL_PACKAGES
from task_manager.registry import get_task_definition, list_task_definitions


RETIRED_TASK_TYPES = {
    "media.script.change.propose",
    "media.calendar.official_date.lookup",
    "media.topic.history_viral.variants.generate",
    "media.topic.industry_calendar.copy.generate",
    "analytics.douyin.content_analysis.batch",
    "form.smart_fill.extract",
}

RETIRED_SKILL_PACKAGES = {
    "main-agent-orchestration-package",
    "media-script-change-proposal-package",
    "media-calendar-official-date-lookup-package",
    "media-history-viral-topic-variants-package",
    "media-industry-calendar-topic-copy-package",
    "douyin-content-analysis-package",
    "smart-fill-project-package",
    "smart-fill-company-package",
    "smart-fill-financial-package",
    "smart-fill-risk-package",
    "smart-fill-analysis-package",
}


def test_non_frontend_task_types_are_not_registered():
    registered = {item.task_type for item in list_task_definitions()}
    assert RETIRED_TASK_TYPES.isdisjoint(registered)
    for task_type in RETIRED_TASK_TYPES:
        with pytest.raises(ValueError, match="Unsupported task_type"):
            get_task_definition(task_type)


def test_non_frontend_skill_packages_are_not_seeded():
    package_names = {item["package_name"] for item in DEFAULT_SKILL_PACKAGES}
    assert RETIRED_SKILL_PACKAGES.isdisjoint(package_names)
