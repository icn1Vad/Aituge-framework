"""Register the minimal Proof policy Q&A capability with Aituge."""

from __future__ import annotations

from pathlib import Path

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CAPABILITY_ID = "proof"
CAPABILITY_DIR = Path(__file__).resolve().parent


class ProofQaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=20)

    @field_validator("question")
    @classmethod
    def non_blank_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized


class ProofSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=20)
    retrieval_mode: str = Field(default="hybrid", pattern=r"^(hybrid|vector|keyword)$")
    policy_ids: list[str] = Field(default_factory=list, max_length=100)
    level_codes: list[str] = Field(default_factory=list, max_length=20)
    category_codes: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("query")
    @classmethod
    def non_blank_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized


class ProofSqlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    sql: str = Field(min_length=1, max_length=20_000)

    @field_validator("question", "sql")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class ProofAuditTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=100_000)
    clause_no: str = Field(default="", max_length=200)
    clause_ordinal: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list, max_length=30)


class ProofAuditBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=240)
    audit_id: str = Field(min_length=1, max_length=160)
    check: Literal["semantic"] = "semantic"
    targets: list[ProofAuditTarget] = Field(min_length=1, max_length=100)


class ProofAuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    items: list[ProofAuditBatch] = Field(min_length=1, max_length=2000)
    max_concurrency: int = Field(default=4, ge=1, le=8)
    failure_policy: Literal["fail_fast"] = "fail_fast"
    retry_per_item: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def validate_chunk_coverage(self):
        ids = [target.id for item in self.items for target in item.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("Audit target chunk IDs must be unique across batches.")
        if any(item.audit_id != self.audit_id for item in self.items):
            raise ValueError("Every batch audit_id must match the task audit_id.")
        return self


class ProofSemanticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=1, max_length=100_000)
    problem: str = Field(min_length=1, max_length=4000)
    suggestion: str = Field(min_length=1, max_length=4000)


class ProofAuditItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ProofSemanticFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_one_finding_per_chunk(self):
        ids = [item.id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("Each chunk may return at most one finding.")
        return self


class ProofAuditBatchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class ProofAuditOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: ProofAuditBatchSummary
    items: list[dict[str, Any]] = Field(default_factory=list)


async def register(registry, settings) -> None:
    """Declare Proof capabilities through the framework-owned registry facade."""

    base_url = settings.require("PROOF_SERVICE_BASE_URL")
    model_id = settings.get("PROOF_QA_MODEL_ID", "deepseek-v4-pro").strip()
    audit_model_id = settings.get("PROOF_AUDIT_MODEL_ID", "deepseek-v4-pro").strip()

    registry.register_skill_root(CAPABILITY_DIR / "skills")
    registry.register_http_tool(
        tool_name="proof_search",
        provider="proof_http",
        display_name="Proof Policy Search",
        description=(
            "Search complete policy clauses with Chinese full-text and pgvector recall plus "
            "optional reranking. Returns stable Chunk citations and retrieval diagnostics."
        ),
        base_url=base_url,
        path="/v1/retrieval/search",
        method="POST",
        input_model=ProofSearchInput,
        timeout_seconds=30,
        max_response_chars=100_000,
    )
    registry.register_http_tool(
        tool_name="proof_sql",
        provider="proof_http",
        display_name="Proof Policy SQL",
        description=(
            "Execute one Agent-generated read-only SQL query over the Proof policy and clause "
            "semantic views. Use for counts, lists, grouping, metadata, and exact filtering."
        ),
        base_url=base_url,
        path="/v1/query/sql",
        method="POST",
        input_model=ProofSqlInput,
        timeout_seconds=10,
        max_response_chars=60_000,
    )
    registry.register_skill_package(
        package_name="proof-policy-qa-package",
        display_name="Proof Policy Q&A",
        description="Evidence-grounded question answering over indexed company policies.",
        tags=["proof", "policy", "qa", "rag"],
        primary_skill="proof-policy-qa",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="proof-qa-agent",
        name="Proof Policy Q&A Agent",
        description="Answers policy questions with Proof retrieval and optional sandbox calculations.",
        agent_type="single",
        model_id=model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Proof policy question-answering agent. Follow the active primary skill. "
            "Search before making policy claims, combine structured facts into one SQL tool call, "
            "preserve uncertainty, and never invent citations."
        ),
        default_tools=["proof_search", "proof_sql", "code_interpreter"],
        default_datasets=[],
    )
    registry.register_task(
        task_type="proof.qa.chat",
        name="Proof Policy Q&A",
        description="Answer one policy question using indexed Proof clauses.",
        handler="scheduler",
        default_agent_id="proof-qa-agent",
        default_skill_package="proof-policy-qa-package",
        default_primary_skill="proof-policy-qa",
        default_tools=["proof_search", "proof_sql", "code_interpreter"],
        default_datasets=[],
        stream_chunk_chars=24,
        conversation_message_field="question",
        input_model=ProofQaInput,
    )
    registry.register_skill_package(
        package_name="proof-policy-semantic-audit-package",
        display_name="Proof Policy Semantic Audit",
        description="Find concrete semantic ambiguity in complete policy chunks.",
        tags=["proof", "policy", "audit", "semantic"],
        primary_skill="proof-policy-semantic-audit",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="proof-audit-agent",
        name="Proof Policy Semantic Audit Agent",
        description="Audits policy chunk batches and returns strict evidence-grounded JSON.",
        agent_type="single",
        model_id=audit_model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Proof semantic audit agent. Follow the active primary skill. "
            "Inspect only target chunks, return strict JSON, return no finding for clear text, "
            "and never invent or paraphrase quote evidence."
        ),
        default_tools=[],
        default_datasets=[],
    )
    registry.register_task(
        task_type="proof.audit.run",
        name="Proof Policy Semantic Audit",
        description="Audit every supplied policy chunk for concrete semantic ambiguity.",
        handler="batch_item_scheduler",
        default_agent_id="proof-audit-agent",
        default_skill_package="proof-policy-semantic-audit-package",
        default_primary_skill="proof-policy-semantic-audit",
        default_tools=[],
        default_datasets=[],
        input_model=ProofAuditInput,
        output_model=ProofAuditOutput,
        item_output_model=ProofAuditItemOutput,
        result_sink_url=f"{base_url.rstrip('/')}/v1/internal/semantic-audits/result",
    )
