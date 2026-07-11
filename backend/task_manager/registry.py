from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_type: str
    name: str
    description: str = ""
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
    pipeline_id: str | None = None


_TASK_DEFINITIONS: dict[str, TaskDefinition] = {
    "pipeline.demo": TaskDefinition(
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
    "media.script.generate": TaskDefinition(
        task_type="media.script.generate",
        name="Media Script Generation",
        description="Generate a short-video script from topic, material, persona, platform, and duration constraints.",
        default_skill_package="media-script-generate-package",
        default_primary_skill="media-script-generator",
        default_candidate_skills=["media-script-selector"],
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
        input_schema_name="media_script_generate_input",
        output_schema_name="media_script_output",
    ),
    "media.script.pipeline.generate": TaskDefinition(
        task_type="media.script.pipeline.generate",
        name="Media Script Pipeline Generation",
        description="Generate and review a short-video script through the staged media Pipeline.",
        handler="pipeline",
        default_agent_id="media-writer-agent",
        default_skill_package="media-script-writer-package",
        default_primary_skill="media-script-writer",
        input_schema_name="media_script_generate_input",
        output_schema_name="media_script_output",
        pipeline_id="media-script-pipeline-v1",
    ),
    "media.script.select": TaskDefinition(
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
    "media.chat": TaskDefinition(
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
    "ai.search.chat": TaskDefinition(
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
    "media.topic.search": TaskDefinition(
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
    "analytics.douyin.account_report.generate": TaskDefinition(
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
    "table.audit": TaskDefinition(
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


def list_task_definitions() -> list[TaskDefinition]:
    return list(_TASK_DEFINITIONS.values())


def get_task_definition(task_type: str) -> TaskDefinition:
    try:
        return _TASK_DEFINITIONS[task_type]
    except KeyError as exc:
        available = ", ".join(sorted(_TASK_DEFINITIONS))
        raise ValueError(f"Unsupported task_type '{task_type}'. Available task types: {available}.") from exc
