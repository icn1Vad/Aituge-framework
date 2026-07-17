from __future__ import annotations

from dataclasses import dataclass, field

from service.conversation import load_durable_conversation_messages
from task_manager.memory import (
    DEFAULT_MEMORY_SKILL_PACKAGE,
    MEMORY_MATERIAL_MAX_CHARS,
    TaskMemoryMaterial,
    TaskMemoryRefreshContext,
    TaskMemoryRefreshResult,
    TaskMemoryService,
)
from task_manager.models import TaskEntity


@dataclass(frozen=True, slots=True)
class TaskType:
    task_type: str
    name: str
    description: str = ""
    required_task_key: str | None = None
    handler: str = "scheduler"
    default_agent_id: str = "default-single-agent"
    default_skill_package: str | None = None
    default_primary_skill: str | None = None
    default_candidate_skills: list[str] = field(default_factory=list)
    default_tools: list[str] = field(default_factory=list)
    default_datasets: list[str] = field(default_factory=list)
    input_schema_name: str | None = None
    output_schema_name: str | None = None
    item_output_schema_name: str | None = None
    result_sink_url: str | None = None
    pipeline_id: str | None = None
    stream_chunk_chars: int = 400
    conversation_message_field: str | None = None

    async def collect_memory_materials(
        self,
        task: TaskEntity,
        context: TaskMemoryRefreshContext,
    ) -> list[TaskMemoryMaterial]:
        return []

    def memory_skill_package(self) -> str:
        return DEFAULT_MEMORY_SKILL_PACKAGE

    async def refresh_memory(
        self,
        task: TaskEntity,
        context: TaskMemoryRefreshContext,
    ) -> TaskMemoryRefreshResult:
        materials = await self.collect_memory_materials(task, context)
        if not materials:
            return TaskMemoryRefreshResult(status="skipped", reason="no_memory_material")
        if not task.task_key:
            return TaskMemoryRefreshResult(status="skipped", reason="no_task_key")
        memory = await TaskMemoryService(context.options).consolidate(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_key=task.task_key,
            materials=materials,
            skill_package=self.memory_skill_package(),
        )
        return TaskMemoryRefreshResult(status="updated", memory=memory)


class ConversationTaskType(TaskType):
    async def collect_memory_materials(
        self,
        task: TaskEntity,
        context: TaskMemoryRefreshContext,
    ) -> list[TaskMemoryMaterial]:
        del context
        if not task.thread_id:
            return []
        history = await load_durable_conversation_messages(
            thread_id=task.thread_id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
        )
        transcript = _recent_conversation_transcript(history)
        if not transcript:
            return []
        return [
            TaskMemoryMaterial(
                kind="conversation",
                content=transcript,
                source={
                    "thread_id": task.thread_id,
                    "session_id": task.session_id,
                    "task_id": task.id,
                },
            )
        ]


def _recent_conversation_transcript(messages: list[dict]) -> str:
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if text:
            rendered.append(f"[{role}]\n{text}")

    selected: list[str] = []
    used_chars = 0
    for item in reversed(rendered):
        separator_chars = 2 if selected else 0
        if used_chars + separator_chars + len(item) > MEMORY_MATERIAL_MAX_CHARS:
            if selected:
                break
            continue
        selected.append(item)
        used_chars += separator_chars + len(item)
    selected.reverse()
    return "\n\n".join(selected)


_TASK_DEFINITIONS: dict[str, TaskType] = {
    "pipeline.demo": TaskType(
        task_type="pipeline.demo",
        name="Pipeline Runtime Demo",
        description="Business-neutral multi-stage task for validating Pipeline runtime and event streaming.",
        handler="pipeline",
        default_agent_id="default-single-agent",
        default_skill_package="pipeline-demo-package",
        default_primary_skill="pipeline-demo",
        input_schema_name="pipeline_demo_input",
        output_schema_name="pipeline_demo_result",
        pipeline_id="pipeline-demo-v1",
    ),
    "media.script.generate": ConversationTaskType(
        task_type="media.script.generate",
        name="Media Script Generation",
        description="Run one formal script Workspace task through MainAgent and its managed specialists.",
        required_task_key="media_script",
        handler="external",
        default_agent_id="main-agent-runtime",
        default_skill_package="media-script-main-agent-package",
        default_primary_skill="media-script-task-orchestration",
        default_candidate_skills=[],
        default_tools=[],
        default_datasets=[],
        input_schema_name="media_script_main_agent_input",
        output_schema_name="media_script_workspace_output",
    ),
    "media.script.pipeline.generate": TaskType(
        task_type="media.script.pipeline.generate",
        name="Media Script Pipeline Generation",
        description="Generate a short-video script and storyboard from provided media context without Agent research or review.",
        handler="pipeline",
        default_agent_id="media-writer-agent",
        default_skill_package="media-script-writer-package",
        default_primary_skill="media-script-writer",
        input_schema_name="media_script_generate_input",
        output_schema_name="media_script_output",
        pipeline_id="media-script-lite-pipeline-v1",
    ),
    "media.script.select": TaskType(
        task_type="media.script.select",
        name="Media Script Selection",
        description="Select the best script candidate and return a structured decision card.",
        default_skill_package="media-script-select-package",
        default_primary_skill="media-script-selector",
        default_candidate_skills=["media-script-generator"],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
        input_schema_name="media_script_select_input",
    ),
    "media.chat": ConversationTaskType(
        task_type="media.chat",
        name="Media Script Chat",
        description="Answer questions about the current media script without mutating artifacts or rerunning its Pipeline.",
        default_skill_package="media-script-chat-package",
        default_primary_skill="media-script-chat",
        default_candidate_skills=[],
        default_tools=[],
        default_datasets=[],
        input_schema_name="media_chat_input",
    ),
    "ai.search.chat": TaskType(
        task_type="ai.search.chat",
        name="AI Search Chat",
        description="Use the configured single agent and web search tool to answer search-oriented user messages.",
        default_skill_package="ai-search-package",
        default_primary_skill="ai-search",
        default_candidate_skills=[],
        default_tools=["web_search"],
        default_datasets=[],
        input_schema_name="ai_search_chat_input",
        output_schema_name="ai_search_output",
    ),
    "media.topic.search": TaskType(
        task_type="media.topic.search",
        name="Media Topic Search",
        description="Search reliable sources and aggregate them into reusable new-media topic suggestions.",
        default_skill_package="media-topic-search-package",
        default_primary_skill="media-topic-search",
        default_candidate_skills=[],
        default_tools=["web_search"],
        default_datasets=[],
        input_schema_name="media_topic_search_input",
        output_schema_name="media_topic_search_output",
    ),
    "analytics.douyin.account_report.generate": TaskType(
        task_type="analytics.douyin.account_report.generate",
        name="Douyin Account Data Report",
        description=(
            "Generate a fact-grounded Douyin account analysis report from all available "
            "account data by default, with optional month or custom range support."
        ),
        default_agent_id="report-agent",
        default_skill_package="douyin-account-report-package",
        default_primary_skill="douyin-account-report",
        default_candidate_skills=[],
        default_tools=[],
        default_datasets=[],
        input_schema_name="douyin_account_report_input",
        output_schema_name="douyin_account_report_output",
    ),
    "table.audit": TaskType(
        task_type="table.audit",
        name="Table Row Audit",
        description="Audit table rows one by one and return item-level results.",
        handler="batch_item_scheduler",
        default_skill_package="table-audit-package",
        default_primary_skill="table-audit",
        default_candidate_skills=[],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
        input_schema_name="table_audit_input",
        output_schema_name="batch_task_output",
        item_output_schema_name="table_audit_item_output",
    ),
}

_TASK_DEFINITION_SOURCES: dict[str, str] = {
    task_type: "framework" for task_type in _TASK_DEFINITIONS
}


def register_task_definition(
    definition: TaskType,
    *,
    source: str,
) -> None:
    """Register one externally owned task without exposing the registry mapping."""

    source_name = source.strip()
    if not source_name:
        raise ValueError("Task definition source is required.")
    if not definition.task_type.strip():
        raise ValueError("Task type is required.")

    existing = _TASK_DEFINITIONS.get(definition.task_type)
    existing_source = _TASK_DEFINITION_SOURCES.get(definition.task_type)
    if existing is not None and existing_source != source_name:
        raise ValueError(
            f"Task type '{definition.task_type}' is already registered by "
            f"'{existing_source or 'framework'}'."
        )

    _TASK_DEFINITIONS[definition.task_type] = definition
    _TASK_DEFINITION_SOURCES[definition.task_type] = source_name


def list_task_definitions() -> list[TaskType]:
    return list(_TASK_DEFINITIONS.values())


def get_task_definition(task_type: str) -> TaskType:
    try:
        return _TASK_DEFINITIONS[task_type]
    except KeyError as exc:
        available = ", ".join(sorted(_TASK_DEFINITIONS))
        raise ValueError(f"Unsupported task_type '{task_type}'. Available task types: {available}.") from exc
