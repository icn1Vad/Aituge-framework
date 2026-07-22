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


class ProofConflictSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=160)
    top_k: int = Field(default=10, ge=1, le=20)

    @field_validator("unit_id")
    @classmethod
    def non_blank_unit_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("unit_id must not be blank")
        return normalized


class ProofIntraConflictSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=160)

    @field_validator("unit_id")
    @classmethod
    def non_blank_unit_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("unit_id must not be blank")
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


class ProofPolicySummaryInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    audit_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    summary_chunks: list[ProofAuditTarget] = Field(min_length=1, max_length=2000)
    summary_max_chars: int = Field(default=60_000, ge=1)

    @model_validator(mode="after")
    def enforce_summary_limit(self):
        if sum(len(item.text) for item in self.summary_chunks) > self.summary_max_chars:
            raise ValueError("Document exceeds PROOF_SUMMARY_MAX_CHARS.")
        return self


class ProofSemanticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    category: Literal["semantic_ambiguity", "executability_gap"]
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


class ProofConcernedRole(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)


class ProofPolicySummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plain_summary: str = Field(min_length=1, max_length=6000)
    purpose: str | None = Field(default=None, min_length=1, max_length=2000)
    scope: list[str] = Field(default_factory=list)
    concerned_roles: list[ProofConcernedRole] = Field(default_factory=list)
    key_rules: list[str] = Field(default_factory=list)


class ProofAuditPipelineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: dict[str, Any] = Field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ProofConflictTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    unit_id: str = Field(min_length=1, max_length=160)
    text: str = Field(default="", max_length=100_000)
    clause_no: str = Field(default="", max_length=200)
    heading_path: list[str] = Field(default_factory=list, max_length=30)


class ProofConflictBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=240)
    audit_id: str = Field(min_length=1, max_length=160)
    check: Literal["conflict"] = "conflict"
    targets: list[ProofConflictTarget] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_target_ids(self):
        ids = [target.id for target in self.targets]
        unit_ids = [target.unit_id for target in self.targets]
        if len(ids) != len(set(ids)) or len(unit_ids) != len(set(unit_ids)):
            raise ValueError("Conflict target IDs and unit IDs must be unique within a batch.")
        if any(target.id != target.unit_id for target in self.targets):
            raise ValueError("Conflict target id must equal unit_id.")
        return self


class ProofConflictAuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(default="", max_length=160)
    items: list[ProofConflictBatch] = Field(min_length=1, max_length=2000)
    max_concurrency: int = Field(default=4, ge=1, le=8)
    failure_policy: Literal["fail_fast"] = "fail_fast"
    retry_per_item: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def validate_target_coverage(self):
        target_ids = [target.id for item in self.items for target in item.targets]
        unit_ids = [target.unit_id for item in self.items for target in item.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Conflict target IDs must be unique across batches.")
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("Conflict unit IDs must be unique across batches.")
        if any(item.audit_id != self.audit_id for item in self.items):
            raise ValueError("Every conflict batch audit_id must match the task audit_id.")
        return self


class ProofAuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    summary_chunks: list[ProofAuditTarget] = Field(min_length=1, max_length=2000)
    summary_max_chars: int = Field(default=60_000, ge=1)
    semantic_items: list[ProofAuditBatch] = Field(min_length=1, max_length=2000)
    conflict_items: list[ProofConflictBatch] = Field(min_length=1, max_length=2000)
    intra_conflict_items: list[ProofConflictBatch] = Field(min_length=1, max_length=2000)
    max_concurrency: int = Field(default=4, ge=1, le=8)
    failure_policy: Literal["fail_fast"] = "fail_fast"
    retry_per_item: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def validate_chunk_coverage(self):
        semantic_ids = [target.id for item in self.semantic_items for target in item.targets]
        if len(semantic_ids) != len(set(semantic_ids)):
            raise ValueError("Audit target chunk IDs must be unique across batches.")
        if any(item.audit_id != self.audit_id for item in self.semantic_items):
            raise ValueError("Every batch audit_id must match the task audit_id.")
        summary_ids = [item.id for item in self.summary_chunks]
        if len(summary_ids) != len(set(summary_ids)) or set(summary_ids) != set(semantic_ids):
            raise ValueError("Summary chunks must cover the same unique chunks as audit batches.")
        conflict_ids = [target.id for item in self.conflict_items for target in item.targets]
        if len(conflict_ids) != len(set(conflict_ids)) or set(conflict_ids) != set(summary_ids):
            raise ValueError("Conflict items must cover every chunk exactly once.")
        if any(item.audit_id != self.audit_id for item in self.conflict_items):
            raise ValueError("Every conflict item audit_id must match the task audit_id.")
        if any(len(item.targets) != 1 for item in self.conflict_items):
            raise ValueError("The integrated conflict stage requires exactly one target per item.")
        intra_ids = [
            target.id for item in self.intra_conflict_items for target in item.targets
        ]
        if len(intra_ids) != len(set(intra_ids)) or set(intra_ids) != set(summary_ids):
            raise ValueError("Intra-policy conflict items must cover every chunk exactly once.")
        if any(item.audit_id != self.audit_id for item in self.intra_conflict_items):
            raise ValueError("Every intra-policy conflict item audit_id must match the task audit_id.")
        if any(len(item.targets) != 1 for item in self.intra_conflict_items):
            raise ValueError("The intra-policy conflict stage requires exactly one target per item.")
        return self


class ProofConflictFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    candidate_ids: list[str] = Field(min_length=1, max_length=4)
    conflict_type: Literal[
        "numeric_conflict",
        "authority_conflict",
        "process_conflict",
        "rule_reversal",
    ]
    problem: str = Field(min_length=1, max_length=4000)
    suggestion: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def normalize_candidate_ids(self):
        self.candidate_ids = list(dict.fromkeys(self.candidate_ids))
        if self.id in self.candidate_ids:
            raise ValueError("Conflict candidate IDs must not contain the source target ID.")
        return self


class ProofConflictItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ProofConflictFinding] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_unique_findings(self):
        keys = [
            (finding.id, tuple(sorted(finding.candidate_ids)), finding.conflict_type)
            for finding in self.findings
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate conflict findings are not allowed.")
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.id] = counts.get(finding.id, 0) + 1
        if any(count > 3 for count in counts.values()):
            raise ValueError("Each target may return at most three conflict findings.")
        return self


async def register(registry, settings) -> None:
    """Declare Proof capabilities through the framework-owned registry facade."""

    base_url = settings.require("PROOF_SERVICE_BASE_URL")
    model_id = settings.get("PROOF_QA_MODEL_ID", "deepseek-v4-pro").strip()
    audit_model_id = settings.get("PROOF_AUDIT_MODEL_ID", "deepseek-v4-pro").strip()
    conflict_model_id = settings.get("PROOF_CONFLICT_MODEL_ID", audit_model_id).strip()

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
    registry.register_http_tool(
        tool_name="proof_conflict_search",
        provider="proof_http",
        display_name="Proof Conflict Evidence Search",
        description=(
            "Retrieve one source policy unit and cross-policy conflict candidates through "
            "same-title, leaf-category, parent-category, and global vector branches, then rerank."
        ),
        base_url=base_url,
        path="/v1/internal/conflict-retrieval",
        method="POST",
        input_model=ProofConflictSearchInput,
        timeout_seconds=45,
        max_response_chars=160_000,
    )
    registry.register_http_tool(
        tool_name="proof_intra_conflict_search",
        provider="proof_http",
        display_name="Proof Intra-policy Conflict Search",
        description=(
            "Return the ten most similar other Chunks from the same draft policy using "
            "temporary audit embeddings."
        ),
        base_url=base_url,
        path="/v1/internal/intra-conflict-retrieval",
        method="POST",
        input_model=ProofIntraConflictSearchInput,
        timeout_seconds=45,
        max_response_chars=160_000,
    )
    registry.register_skill_package(
        package_name="proof-policy-qa-package",
        display_name="Proof Policy Q&A",
        description="Evidence-grounded question answering over indexed company policies.",
        tags=["proof", "policy", "qa", "rag"],
        primary_skill="proof-policy-qa",
        auxiliary_skills=["proof-policy-sql"],
    )
    registry.register_agent(
        agent_id="proof-qa-agent",
        name="Proof Policy Q&A Agent",
        description="Answers policy questions with Proof retrieval and optional code-generated calculations and charts.",
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
    registry.register_skill_package(
        package_name="proof-policy-summary-package",
        display_name="Proof Preliminary Policy Analysis",
        description="Produce a source-grounded preliminary analysis report for one complete policy.",
        tags=["proof", "policy", "summary"],
        primary_skill="proof-policy-summary",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="proof-summary-agent",
        name="Proof Preliminary Policy Analysis Agent",
        description="Reads a complete policy once and returns a clear source-grounded preliminary analysis report.",
        agent_type="single",
        model_id=audit_model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Proof preliminary policy analysis agent. Read the complete supplied policy before writing. "
            "Produce a clear multi-section reader-facing analysis report while explaining only what the policy "
            "states. Do not perform semantic or conflict auditing, infer missing rules, or claim compliance. "
            "Return strict source-grounded JSON."
        ),
        default_tools=[],
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
        display_name="Proof Policy Clarity and Executability Audit",
        description="Find material semantic ambiguity and executability gaps in policy chunks.",
        tags=["proof", "policy", "audit", "semantic", "executability"],
        primary_skill="proof-policy-semantic-audit",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="proof-audit-agent",
        name="Proof Policy Clarity and Executability Audit Agent",
        description="Audits policy chunks for clarity and executability with strict source-grounded JSON.",
        agent_type="single",
        model_id=audit_model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Proof policy clarity and executability audit agent. Follow the active primary skill. "
            "Inspect only target chunks, return strict JSON, return no finding for clear text, "
            "and identify findings only by the supplied target chunk IDs."
        ),
        default_tools=[],
        default_datasets=[],
    )
    registry.register_task(
        task_type="proof.audit.run",
        name="Proof Policy Review Report",
        description="Summarize a policy and run semantic, cross-policy, and intra-policy audits in one pipeline.",
        handler="pipeline",
        pipeline_id="proof-audit-pipeline-v1",
        default_agent_id="proof-summary-agent",
        default_skill_package="proof-policy-summary-package",
        default_primary_skill="proof-policy-summary",
        default_tools=[],
        default_datasets=[],
        input_model=ProofAuditInput,
        output_model=ProofAuditPipelineOutput,
        result_sink_url=f"{base_url.rstrip('/')}/v1/internal/semantic-audits/result",
    )
    registry.register_pipeline(
        pipeline_id="proof-audit-pipeline-v1",
        version="1.2",
        task_type="proof.audit.run",
        description="Run summary, semantic, cross-policy, and intra-policy audits concurrently.",
        final_artifact_type="proof_audit_result",
        max_parallelism=4,
        stages=[
            {
                "stage_id": "policy_summary",
                "name": "Read and summarize complete policy",
                "stage_type": "direct_model",
                "input_model": ProofPolicySummaryInput,
                "output_model": ProofPolicySummaryOutput,
                "input_adapter": "task_input",
                "artifact_type": "proof_policy_summary",
                "agent_id": "proof-summary-agent",
                "skill_package": "proof-policy-summary-package",
                "output_policy": "repair_once",
                "timeout_seconds": 180,
                "retry_policy": {"max_attempts": 2, "retry_on": ["invalid_output", "timeout"]},
                "failure_policy": "continue_with_warning",
            },
            {
                "stage_id": "semantic_audit",
                "name": "Audit policy chunks",
                "stage_type": "batch",
                "item_source": "semantic_items",
                "output_model": ProofAuditOutput,
                "item_output_model": ProofAuditItemOutput,
                "artifact_type": "proof_semantic_audit",
                "agent_id": "proof-audit-agent",
                "skill_package": "proof-policy-semantic-audit-package",
                "timeout_seconds": 900,
                "failure_policy": "fail_task",
            },
            {
                "stage_id": "conflict_audit",
                "name": "Audit cross-policy rule conflicts",
                "stage_type": "batch",
                "item_source": "conflict_items",
                "output_model": ProofAuditOutput,
                "item_output_model": ProofConflictItemOutput,
                "artifact_type": "proof_conflict_audit",
                "agent_id": "proof-conflict-agent",
                "skill_package": "proof-policy-conflict-audit-package",
                "tools": ["proof_conflict_search"],
                "timeout_seconds": 900,
                "failure_policy": "continue_with_warning",
            },
            {
                "stage_id": "intra_conflict_audit",
                "name": "Audit conflicts between Chunks in the current policy",
                "stage_type": "batch",
                "item_source": "intra_conflict_items",
                "output_model": ProofAuditOutput,
                "item_output_model": ProofConflictItemOutput,
                "artifact_type": "proof_intra_conflict_audit",
                "agent_id": "proof-intra-conflict-agent",
                "skill_package": "proof-policy-intra-conflict-audit-package",
                "tools": ["proof_intra_conflict_search"],
                "timeout_seconds": 900,
                "failure_policy": "fail_task",
            },
            {
                "stage_id": "finalize_report",
                "name": "Merge policy review artifacts",
                "stage_type": "finalizer",
                "depends_on": [
                    "policy_summary",
                    "semantic_audit",
                    "conflict_audit",
                    "intra_conflict_audit",
                ],
                "output_model": ProofAuditPipelineOutput,
                "artifact_type": "proof_audit_result",
                "service_handler": "merge_pipeline_artifacts",
                "timeout_seconds": 30,
            },
        ],
    )
    registry.register_skill_package(
        package_name="proof-policy-conflict-audit-package",
        display_name="Proof Policy Conflict Audit",
        description="Evidence-grounded joint conflict detection over policy units and retrieved rules.",
        tags=["proof", "policy", "audit", "conflict", "rag"],
        primary_skill="proof-policy-conflict-audit",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="proof-conflict-agent",
        name="Proof Policy Conflict Audit Agent",
        description="Retrieves conflict evidence and jointly checks numeric, authority, process, and polarity rules.",
        agent_type="single",
        model_id=conflict_model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Proof policy conflict audit agent. Follow the active primary skill. "
            "Call proof_conflict_search exactly once for every target unit, compare only supplied "
            "source-grounded rules, copy source/result chunk IDs exactly from tool output, suppress "
            "differences that can coexist, and return only IDs, conflict type, problem, and suggestion."
        ),
        default_tools=["proof_conflict_search"],
        default_datasets=[],
    )
    registry.register_skill_package(
        package_name="proof-policy-intra-conflict-audit-package",
        display_name="Proof Intra-policy Chunk Conflict Audit",
        description="Detect material conflicts between different Chunks of the current policy.",
        tags=["proof", "policy", "audit", "intra-conflict", "rag"],
        primary_skill="proof-policy-intra-conflict-audit",
        auxiliary_skills=[],
    )
    registry.register_agent(
        agent_id="proof-intra-conflict-agent",
        name="Proof Intra-policy Chunk Conflict Audit Agent",
        description="Checks the current source Chunk against same-policy vector candidates.",
        agent_type="single",
        model_id=conflict_model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Proof intra-policy Chunk conflict audit agent. Follow the active "
            "primary skill. Call proof_intra_conflict_search exactly once for the current "
            "target, compare only different Chunks returned by the tool, copy IDs exactly, "
            "suppress compatible differences, and return only the five finding fields."
        ),
        default_tools=["proof_intra_conflict_search"],
        default_datasets=[],
    )
    registry.register_task(
        task_type="proof.conflict.audit",
        name="Proof Policy Conflict Audit",
        description="Retrieve evidence and audit supplied policy units for material rule conflicts.",
        handler="batch_item_scheduler",
        default_agent_id="proof-conflict-agent",
        default_skill_package="proof-policy-conflict-audit-package",
        default_primary_skill="proof-policy-conflict-audit",
        default_tools=["proof_conflict_search"],
        default_datasets=[],
        input_model=ProofConflictAuditInput,
        output_model=ProofAuditOutput,
        item_output_model=ProofConflictItemOutput,
        result_sink_url=f"{base_url.rstrip('/')}/v1/internal/conflict-audits/result",
    )
