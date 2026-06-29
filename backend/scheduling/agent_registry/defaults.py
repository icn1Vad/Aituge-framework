from __future__ import annotations

import json
from typing import Any

from .models import AgentProfileEntity


REPORT_SKILLS = [
    "report-generator",
    "report-context-scope",
    "report-executive-summary",
    "report-analysis-findings",
    "report-quantitative-calculation",
    "report-chart-figure",
    "report-code-verification",
    "report-risk-actions",
]

GENERAL_SKILLS = [
    "task-style",
    "implementation-plan",
    "debugging-checklist",
    "review-style",
    "concise-summary",
]

ALL_CAPABILITY_TOOLS = [
    "code_interpreter",
    "enabled_db_tools",
    "rag_retrieval",
]

ALL_CAPABILITY_SKILLS = GENERAL_SKILLS + REPORT_SKILLS

DEFAULT_SINGLE_AGENT_PROMPT = (
    "You are Default Single Agent, a general-purpose TUGE single agent. "
    "Use your configured tools, skills, and datasets to answer the user's task."
)
REPORT_AGENT_PROMPT = (
    "You are Report Agent, a TUGE single agent specialized in complete, "
    "evidence-grounded reports."
)
ALL_CAPABLE_AGENT_PROMPT = (
    "You are All Capable Agent, a TUGE single agent used for integration testing "
    "with every currently registered local capability enabled by allowlist."
)
RAG_AGENT_PROMPT = (
    "You are RAG Agent, a TUGE single agent specialized in retrieving and "
    "answering from the configured local knowledge base."
)
CODE_AGENT_PROMPT = (
    "You are Code Agent, a TUGE single agent specialized in local Python "
    "execution, calculation, verification, and artifact generation."
)


def _profile_definition(
    agent_id: str,
    name: str,
    description: str,
    default_tools: list[str],
    default_skills: list[str] | None = None,
    default_datasets: list[str] | None = None,
    model_id: str = "deepseek-v4-pro",
    system_prompt: str = "",
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "name": name,
        "description": description,
        "model_id": model_id,
        "system_prompt": system_prompt,
        "default_tools_json": json.dumps(default_tools, ensure_ascii=True),
        "default_skills_json": json.dumps(default_skills or [], ensure_ascii=True),
        "default_datasets_json": json.dumps(default_datasets or [], ensure_ascii=True),
        "runtime_config_json": json.dumps(runtime_config or {}, ensure_ascii=True),
    }


DEFAULT_AGENT_PROFILE_DEFINITIONS = [
    _profile_definition(
        agent_id="default-single-agent",
        name="Default Single Agent",
        description="General-purpose single agent with code, DB-enabled tools, and local RAG retrieval.",
        default_tools=["code_interpreter", "enabled_db_tools", "rag_retrieval"],
        default_datasets=["local_rag"],
        system_prompt=DEFAULT_SINGLE_AGENT_PROMPT,
    ),
    _profile_definition(
        agent_id="report-agent",
        name="Report Agent",
        description="Single agent preloaded with report-writing skills plus code, search, and RAG tools.",
        default_tools=["code_interpreter", "enabled_db_tools", "rag_retrieval"],
        default_skills=REPORT_SKILLS,
        default_datasets=["local_rag"],
        system_prompt=REPORT_AGENT_PROMPT,
    ),
    _profile_definition(
        agent_id="all-capable-agent",
        name="All Capable Agent",
        description="Single agent with every currently registered local capability enabled by allowlist.",
        default_tools=ALL_CAPABILITY_TOOLS,
        default_skills=ALL_CAPABILITY_SKILLS,
        default_datasets=["local_rag"],
        system_prompt=ALL_CAPABLE_AGENT_PROMPT,
        runtime_config={"tool_policy": "allowlist", "skill_policy": "allowlist"},
    ),
    _profile_definition(
        agent_id="rag-agent",
        name="RAG Agent",
        description="Single agent focused on local knowledge-base retrieval.",
        default_tools=["rag_retrieval"],
        default_datasets=["local_rag"],
        system_prompt=RAG_AGENT_PROMPT,
    ),
    _profile_definition(
        agent_id="code-agent",
        name="Code Agent",
        description="Single agent focused on local Python execution and artifact generation.",
        default_tools=["code_interpreter"],
        system_prompt=CODE_AGENT_PROMPT,
    ),
]


def build_default_agent_profiles() -> list[AgentProfileEntity]:
    return [
        AgentProfileEntity(**definition)
        for definition in DEFAULT_AGENT_PROFILE_DEFINITIONS
    ]
