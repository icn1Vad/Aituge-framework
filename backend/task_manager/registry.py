from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_type: str
    name: str
    description: str = ""
    handler: str = "scheduler"
    default_agent_id: str = "default-single-agent"
    default_primary_skill: str | None = None
    default_candidate_skills: list[str] = field(default_factory=list)
    default_tools: list[str] = field(default_factory=list)
    default_datasets: list[str] = field(default_factory=list)


_TASK_DEFINITIONS: dict[str, TaskDefinition] = {
    "media.script.generate": TaskDefinition(
        task_type="media.script.generate",
        name="Media Script Generation",
        description="Generate a short-video script from topic, material, persona, platform, and duration constraints.",
        default_primary_skill="media-script-generator",
        default_candidate_skills=["media-script-selector"],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
    ),
    "media.script.select": TaskDefinition(
        task_type="media.script.select",
        name="Media Script Selection",
        description="Select the best script candidate and return a structured decision card.",
        default_primary_skill="media-script-selector",
        default_candidate_skills=["media-script-generator"],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
    ),
    "media.chat": TaskDefinition(
        task_type="media.chat",
        name="Media Task Chat",
        description="Continue a media task conversation with TaskManager lifecycle and event recording.",
        default_primary_skill="media-script-generator",
        default_candidate_skills=["media-script-selector"],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
    ),
    "table.audit": TaskDefinition(
        task_type="table.audit",
        name="Table Row Audit",
        description="Audit table rows one by one and return item-level results.",
        handler="batch_item_scheduler",
        default_primary_skill="table-audit",
        default_candidate_skills=[],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
    ),
}


def list_task_definitions() -> list[TaskDefinition]:
    return list(_TASK_DEFINITIONS.values())


def get_task_definition(task_type: str) -> TaskDefinition:
    try:
        return _TASK_DEFINITIONS[task_type]
    except KeyError as exc:
        available = ", ".join(sorted(_TASK_DEFINITIONS))
        raise ValueError(f"Unsupported task_type '{task_type}'. Available task types: {available}.") from exc
