"""Register the Qian Xuesen mentor capability with Aituge Framework."""

from __future__ import annotations

from pathlib import Path

from aituge_model.config import ModelRuntimeProvider
from pydantic import BaseModel, ConfigDict, Field, field_validator


CAPABILITY_ID = "qianxuesen-mentor"
CAPABILITY_DIR = Path(__file__).resolve().parent


class QianXuesenQaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=12, ge=1, le=20)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


class QianXuesenSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=12, ge=1, le=20)
    retrieval_mode: str = Field(default="hybrid", pattern=r"^(hybrid|keyword|vector)$")


class QianXuesenSqlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    sql: str = Field(min_length=1, max_length=20_000)


async def register(registry, settings) -> None:
    base_url = settings.require("QXS_SERVICE_BASE_URL")
    runtime = ModelRuntimeProvider.from_environment(
        directory=settings.get("MODEL_CONFIG_DIR"), pack_id=settings.get("MODEL_PACK_ID")
    )
    registry.register_skill_root(CAPABILITY_DIR / "skills")
    registry.register_http_tool(
        tool_name="qxs_retrieve", provider="qxs_http", display_name="钱学森三层知识检索",
        description=(
            "并行检索确定信息、工程方法卡和书籍原文，返回书名、章节、页码、原文和证据类型。"
            "回答钱学森相关事实、原话、思想和工程方法前必须调用。"
        ),
        base_url=base_url, path="/v1/retrieval/search", method="POST",
        input_model=QianXuesenSearchInput, timeout_seconds=45, max_response_chars=160_000,
    )
    registry.register_http_tool(
        tool_name="qxs_sql", provider="qxs_http", display_name="钱学森知识库精确查询",
        description=(
            "执行一条只读 SQL，仅可访问 qxs_sql_document_v、qxs_sql_fact_v、"
            "qxs_sql_principle_v；用于计数、时间线、分组和精确筛选。"
        ),
        base_url=base_url, path="/v1/query/sql", method="POST",
        input_model=QianXuesenSqlInput, timeout_seconds=10, max_response_chars=60_000,
    )
    registry.register_skill_package(
        package_name="qianxuesen-mentor-package", display_name="钱学森导师问答",
        description="以钱学森资料和系统工程方法开展自然的导师式问答。",
        tags=["钱学森", "教育", "系统工程", "rag"],
        primary_skill="qianxuesen-mentor-qa", auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="qianxuesen-mentor-agent", name="钱学森导师智能体",
        description="基于钱学森资料，以严谨、克制、系统化的第一人称风格回答。",
        agent_type="single", model_id=runtime.active_pack.llm.id,
        system_prompt=(
            "以钱学森式的科学家导师口吻自然交谈：冷静、朴实、有判断力，善于从系统和实践中"
            "点出关键，也能说出凝练而有启发性的句子。直接进入问题，不作身份说明，不套固定模板。"
            "资料检索在后台完成，把可靠资料化成自己的理解来谈。"
        ),
        default_tools=["qxs_retrieve", "qxs_sql", "code_interpreter", "web_search"],
        default_datasets=[],
    )
    registry.register_resource_task(
        resource_pool="qianxuesen-library", access_mode="read",
        task_type="qianxuesen.qa.chat", name="钱学森导师问答",
        description="检索钱学森资料并进行自然、有启发性的导师式问答。",
        handler="scheduler", default_agent_id="qianxuesen-mentor-agent",
        default_skill_package="qianxuesen-mentor-package",
        default_primary_skill="qianxuesen-mentor-qa",
        default_tools=["qxs_retrieve", "qxs_sql", "code_interpreter", "web_search"],
        default_datasets=[], stream_chunk_chars=24,
        conversation_message_field="question", input_model=QianXuesenQaInput,
    )
