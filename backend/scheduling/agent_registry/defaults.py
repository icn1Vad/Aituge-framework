from __future__ import annotations

import json
from typing import Any

from common.llm.constants import DEFAULT_LLM_MODEL_ID

from .models import AgentProfileEntity


ALL_CAPABILITY_TOOLS = [
    "code_interpreter",
    "enabled_db_tools",
    "rag_retrieval",
]

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
MEDIA_RESEARCH_AGENT_PROMPT = (
    "You are Media Research Agent. Build a factual evidence package for one short-video script task. "
    "Use only the supplied business context and allowed search tool; do not write the final script."
)
LEGACY_MEDIA_WRITER_AGENT_PROMPT = (
    "You are Media Writer Agent. Write one complete speakable short-video script from verified research, "
    "persona, and master-library context. Do not perform unrelated research."
)
LEGACY_MEDIA_STORYBOARD_AGENT_PROMPT = (
    "You are Media Storyboard Agent. Convert the approved script draft into executable shots while "
    "preserving narration, persona, timing, and visual continuity."
)
MEDIA_WRITER_AGENT_PROMPT = "You are Media Writer Agent."
MEDIA_STORYBOARD_AGENT_PROMPT = "You are Media Storyboard Agent."
MEDIA_REVIEW_AGENT_PROMPT = (
    "You are Media Review Agent. Review script, storyboard, evidence, and deterministic findings for "
    "compliance, factual risk, quality, and production feasibility."
)


def _profile_definition(
    agent_id: str,
    name: str,
    description: str,
    default_tools: list[str],
    default_datasets: list[str] | None = None,
    model_id: str = DEFAULT_LLM_MODEL_ID,
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
        default_datasets=["local_rag"],
        system_prompt=REPORT_AGENT_PROMPT,
    ),
    _profile_definition(
        agent_id="all-capable-agent",
        name="All Capable Agent",
        description="Single agent with every currently registered local capability enabled by allowlist.",
        default_tools=ALL_CAPABILITY_TOOLS,
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
    _profile_definition(
        agent_id="media-research-agent",
        name="Media Research Agent",
        description="Evidence research for the media script Pipeline.",
        default_tools=["web_search"],
        system_prompt=MEDIA_RESEARCH_AGENT_PROMPT,
    ),
    _profile_definition(
        agent_id="media-writer-agent",
        name="Media Writer Agent",
        description="Structured short-video script writer.",
        default_tools=[],
        system_prompt=MEDIA_WRITER_AGENT_PROMPT,
        runtime_config={
            "delegation": {
                "enabled": True,
                "use_when": "A complete or revised speakable media script must be saved to the Workspace.",
                "modes": {
                    "consult": {
                        "skill_package": "media-writer-consult-package",
                        "workspace_tools": ["read_script_workspace"],
                    },
                    "delegate": {
                        "skill_package": "media-writer-delegate-package",
                        "extra_tools": ["media_master_library"],
                        "workspace_tools": [
                            "read_script_workspace",
                            "write_script_workspace",
                        ],
                        "required_success_tool": "write_script_workspace",
                    },
                },
            }
        },
    ),
    _profile_definition(
        agent_id="media-storyboard-agent",
        name="Media Storyboard Agent",
        description="Executable storyboard generator for media scripts.",
        default_tools=[],
        system_prompt=MEDIA_STORYBOARD_AGENT_PROMPT,
        runtime_config={
            "delegation": {
                "enabled": True,
                "use_when": "The latest Workspace script must be converted into an executable shot list.",
                "modes": {
                    "consult": {
                        "skill_package": "media-storyboard-consult-package",
                        "workspace_tools": ["read_script_workspace"],
                    },
                    "delegate": {
                        "skill_package": "media-storyboard-delegate-package",
                        "workspace_tools": [
                            "read_script_workspace",
                            "write_storyboard_workspace",
                        ],
                        "required_success_tool": "write_storyboard_workspace",
                    },
                },
            }
        },
    ),
    _profile_definition(
        agent_id="media-review-agent",
        name="Media Review Agent",
        description="Compliance, quality, and storyboard reviewer.",
        default_tools=[],
        system_prompt=MEDIA_REVIEW_AGENT_PROMPT,
    ),
]


LEGACY_DEFAULT_SYSTEM_PROMPTS = {
    "media-writer-agent": {LEGACY_MEDIA_WRITER_AGENT_PROMPT},
    "media-storyboard-agent": {LEGACY_MEDIA_STORYBOARD_AGENT_PROMPT},
}

# Default profile migrations are limited to values previously owned by this
# registry. Explicitly configured non-default tool lists remain untouched.
LEGACY_DEFAULT_TOOLS = {
    "media-writer-agent": {(), ("media_master_library",)},
}


def build_default_agent_profiles() -> list[AgentProfileEntity]:
    return [
        AgentProfileEntity(**definition)
        for definition in DEFAULT_AGENT_PROFILE_DEFINITIONS
    ]
