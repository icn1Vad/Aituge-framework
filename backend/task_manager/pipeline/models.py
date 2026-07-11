from __future__ import annotations

from dataclasses import dataclass, field


STAGE_TYPES = {"agent", "deterministic", "gateway", "finalizer"}
FAILURE_POLICIES = {"fail_task", "continue_with_warning", "require_human", "skip_stage"}
OUTPUT_POLICIES = {"strict", "repair_once", "accept_raw"}
SESSION_POLICIES = {"isolated_stage", "reuse_previous_attempt", "reuse_named_session"}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0
    retry_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentStageConfig:
    agent_id: str
    skill_package: str | None = None
    primary_skill: str | None = None
    candidate_skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    session_policy: str = "isolated_stage"
    output_policy: str = "strict"


@dataclass(frozen=True, slots=True)
class StageDefinition:
    stage_id: str
    name: str
    stage_type: str
    depends_on: tuple[str, ...] = ()
    input_schema: str | None = None
    output_schema: str | None = None
    input_adapter: str | None = None
    artifact_type: str | None = None
    timeout_seconds: int = 300
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    failure_policy: str = "fail_task"
    agent_config: AgentStageConfig | None = None
    service_handler: str | None = None
    requires_human_review: bool = False


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    pipeline_id: str
    version: str
    task_type: str
    stages: tuple[StageDefinition, ...]
    description: str = ""
    final_artifact_type: str = "pipeline_result"
    timeout_seconds: int = 900
    resumable: bool = True
    max_parallelism: int = 1

    def ordered_stages(self) -> tuple[StageDefinition, ...]:
        validate_pipeline_definition(self)
        remaining = {stage.stage_id: stage for stage in self.stages}
        resolved: set[str] = set()
        ordered: list[StageDefinition] = []
        while remaining:
            ready = [stage for stage in remaining.values() if set(stage.depends_on) <= resolved]
            if not ready:
                raise ValueError(f"Pipeline '{self.pipeline_id}' contains a dependency cycle.")
            for stage in ready:
                ordered.append(stage)
                resolved.add(stage.stage_id)
                remaining.pop(stage.stage_id)
        return tuple(ordered)


def validate_pipeline_definition(definition: PipelineDefinition) -> None:
    if not definition.pipeline_id or not definition.version or not definition.task_type:
        raise ValueError("Pipeline id, version, and task_type are required.")
    if not definition.stages:
        raise ValueError(f"Pipeline '{definition.pipeline_id}' has no stages.")
    if definition.max_parallelism < 1:
        raise ValueError("max_parallelism must be at least 1.")

    ids = [stage.stage_id for stage in definition.stages]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Pipeline '{definition.pipeline_id}' contains duplicate stage ids.")
    known = set(ids)
    for stage in definition.stages:
        if stage.stage_type not in STAGE_TYPES:
            raise ValueError(f"Stage '{stage.stage_id}' has unsupported type '{stage.stage_type}'.")
        if stage.failure_policy not in FAILURE_POLICIES:
            raise ValueError(f"Stage '{stage.stage_id}' has invalid failure policy '{stage.failure_policy}'.")
        if stage.timeout_seconds <= 0:
            raise ValueError(f"Stage '{stage.stage_id}' timeout must be positive.")
        if stage.retry_policy.max_attempts < 1:
            raise ValueError(f"Stage '{stage.stage_id}' max_attempts must be at least 1.")
        missing = set(stage.depends_on) - known
        if missing:
            raise ValueError(f"Stage '{stage.stage_id}' depends on unknown stages: {sorted(missing)}.")
        if stage.stage_id in stage.depends_on:
            raise ValueError(f"Stage '{stage.stage_id}' cannot depend on itself.")
        if stage.stage_type == "agent":
            if stage.agent_config is None:
                raise ValueError(f"Agent stage '{stage.stage_id}' requires agent_config.")
            if stage.agent_config.session_policy not in SESSION_POLICIES:
                raise ValueError(f"Agent stage '{stage.stage_id}' has invalid session policy.")
            if stage.agent_config.output_policy not in OUTPUT_POLICIES:
                raise ValueError(f"Agent stage '{stage.stage_id}' has invalid output policy.")
        elif not stage.service_handler:
            raise ValueError(f"Stage '{stage.stage_id}' requires service_handler.")

    # Force cycle validation without recursively calling ordered_stages().
    remaining = {stage.stage_id: stage for stage in definition.stages}
    resolved: set[str] = set()
    while remaining:
        ready = [stage for stage in remaining.values() if set(stage.depends_on) <= resolved]
        if not ready:
            raise ValueError(f"Pipeline '{definition.pipeline_id}' contains a dependency cycle.")
        for stage in ready:
            resolved.add(stage.stage_id)
            remaining.pop(stage.stage_id)
