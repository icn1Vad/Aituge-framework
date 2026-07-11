from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService

from task_manager.handlers.base import TaskHandlerEvent
from task_manager.models import TaskArtifactEntity, TaskEntity, TaskRunEntity, utc_now
from task_manager.output_parser import parse_json_output
from task_manager.payload_schemas import validate_stage_payload

from .errors import PipelineCancelled, StageExecutionError
from .models import PipelineDefinition, StageDefinition
from .stage_registry import StageExecutionContext, StageServiceResult, get_stage_handler
from .store import (
    create_artifact,
    create_stage_run,
    get_run,
    list_artifacts,
    list_stage_runs,
    update_run,
    update_stage_run,
)


class PipelineExecutor:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def stream(
        self,
        *,
        task: TaskEntity,
        run: TaskRunEntity,
        definition: PipelineDefinition,
    ) -> AsyncIterator[TaskHandlerEvent]:
        ordered = definition.ordered_stages()
        artifacts = await _artifacts_by_stage(run.id)
        existing_stage_runs = await list_stage_runs(run.id)
        attempt_offsets: dict[str, int] = {}
        for existing in existing_stage_runs:
            attempt_offsets[existing.stage_id] = max(
                attempt_offsets.get(existing.stage_id, 0),
                existing.attempt,
            )
        resume_from = str((run.metadata_json or {}).get("resume_from_stage") or "")
        resume_reached = not resume_from

        yield _event(
            "pipeline_started",
            "pipeline",
            "Pipeline started.",
            payload={"pipeline_id": definition.pipeline_id, "version": definition.version},
        )

        for stage_index, stage in enumerate(ordered, start=1):
            await _raise_if_cancelled(run.id)
            if not resume_reached:
                if stage.stage_id == resume_from:
                    resume_reached = True
                elif stage.stage_id in artifacts:
                    continue
            await update_run(run.id, current_stage_id=stage.stage_id, status="running")

            stage_input, input_artifacts = _build_stage_input(task, stage, artifacts)
            try:
                stage_input = validate_stage_payload(stage.input_schema, stage_input)
            except ValueError as exc:
                raise StageExecutionError(str(exc), code="invalid_stage_input") from exc

            result: StageServiceResult | None = None
            last_error: StageExecutionError | None = None
            for local_attempt in range(1, stage.retry_policy.max_attempts + 1):
                attempt = attempt_offsets.get(stage.stage_id, 0) + local_attempt
                result = None
                await _raise_if_cancelled(run.id)
                stage_run = await create_stage_run(
                    task_id=task.id,
                    run_id=run.id,
                    stage_id=stage.stage_id,
                    stage_type=stage.stage_type,
                    attempt=attempt,
                    agent_id=stage.agent_config.agent_id if stage.agent_config else None,
                    input_artifact_ids=[item.id for item in input_artifacts],
                )
                yield _event(
                    "stage_started",
                    stage.stage_id,
                    f"Stage '{stage.name}' started.",
                    stage_run_id=stage_run.id,
                    agent_id=stage_run.agent_id,
                    payload={"stage_type": stage.stage_type, "attempt": attempt, "stage_index": stage_index},
                )
                started = time.perf_counter()
                try:
                    async with asyncio.timeout(stage.timeout_seconds):
                        if stage.stage_type == "agent":
                            async for item in self._stream_agent_stage(
                                task=task,
                                run=run,
                                stage=stage,
                                stage_run_id=stage_run.id,
                                attempt=attempt,
                                stage_input=stage_input,
                            ):
                                await _raise_if_cancelled(run.id)
                                if item.structured_output is not None:
                                    result = StageServiceResult(output=item.structured_output)
                                yield item
                        else:
                            handler = get_stage_handler(stage.service_handler or "")
                            result = await handler(
                                StageExecutionContext(
                                    task=task,
                                    run=(await get_run(run.id)) or run,
                                    stage=stage,
                                    stage_run=stage_run,
                                    stage_input=stage_input,
                                    artifacts=artifacts,
                                )
                            )
                    if result is None:
                        raise StageExecutionError("Stage produced no result.", code="empty_stage_output")
                    validated = validate_stage_payload(stage.output_schema, result.output)
                    result.output = validated
                    break
                except TimeoutError as exc:
                    last_error = StageExecutionError("Stage timed out.", code="timeout", retryable=True)
                    result = None
                    await _mark_stage_failed(stage_run.id, started, last_error)
                except StageExecutionError as exc:
                    last_error = exc
                    result = None
                    await _mark_stage_failed(stage_run.id, started, exc)
                except ValueError as exc:
                    last_error = StageExecutionError(str(exc), code="invalid_output", retryable=True)
                    result = None
                    await _mark_stage_failed(stage_run.id, started, last_error)
                except PipelineCancelled:
                    await update_stage_run(
                        stage_run.id,
                        status="cancelled",
                        finished_at=utc_now(),
                        duration_ms=_duration_ms(started),
                    )
                    raise
                except Exception as exc:
                    last_error = StageExecutionError(str(exc), code=exc.__class__.__name__, retryable=False)
                    result = None
                    await _mark_stage_failed(stage_run.id, started, last_error)

                yield _event(
                    "stage_failed",
                    stage.stage_id,
                    str(last_error),
                    level="error",
                    stage_run_id=stage_run.id,
                    agent_id=stage_run.agent_id,
                    payload={"attempt": attempt, "error_code": last_error.code},
                )
                if local_attempt < stage.retry_policy.max_attempts and _can_retry(stage, last_error):
                    yield _event(
                        "stage_retrying",
                        stage.stage_id,
                        f"Retrying stage '{stage.name}'.",
                        stage_run_id=stage_run.id,
                        payload={"next_attempt": attempt + 1, "backoff_seconds": stage.retry_policy.backoff_seconds},
                    )
                    if stage.retry_policy.backoff_seconds:
                        await asyncio.sleep(stage.retry_policy.backoff_seconds)
                    continue
                break

            if result is None:
                assert last_error is not None
                if stage.failure_policy in {"continue_with_warning", "skip_stage"}:
                    yield _event(
                        "stage_skipped",
                        stage.stage_id,
                        f"Stage skipped after failure: {last_error}",
                        level="warning",
                        payload={"failure_policy": stage.failure_policy},
                    )
                    continue
                if stage.failure_policy == "require_human":
                    yield _event(
                        "pipeline_paused",
                        stage.stage_id,
                        "Pipeline paused after a stage failure.",
                        level="warning",
                        payload={"reason": str(last_error)},
                    )
                    yield _human_review_event(stage, None, str(last_error), terminal=True)
                    return
                raise last_error

            latest_run = (await get_run(run.id)) or run
            review = dict(latest_run.metadata_json or {}).get("human_review") or {}
            approved_stage = (
                review.get("action") == "approve"
                and (review.get("resume_from_stage") or latest_run.current_stage_id) == stage.stage_id
            )
            if stage.requires_human_review and not approved_stage:
                result.pause = True
                result.pause_reason = result.pause_reason or f"Stage '{stage.name}' requires human approval."
                result.pause_payload = {
                    "reason_codes": ["stage_requires_human_review"],
                    "allowed_actions": ["approve", "reject", "revise_input", "rerun_stage"],
                    **result.pause_payload,
                }

            artifact = await create_artifact(
                task_id=task.id,
                run_id=run.id,
                stage_run_id=stage_run.id,
                artifact_type=stage.artifact_type or f"{stage.stage_id}_result",
                schema_name=stage.output_schema or "",
                content=result.output,
                parent_artifact_ids=[item.id for item in input_artifacts],
                summary=result.summary,
                metadata=result.metadata,
            )
            artifacts[stage.stage_id] = artifact
            await update_stage_run(
                stage_run.id,
                status="waiting_human" if result.pause else "succeeded",
                output_artifact_id=artifact.id,
                finished_at=utc_now(),
                duration_ms=_duration_ms(started),
            )
            yield _event(
                "artifact_created",
                stage.stage_id,
                f"Artifact '{artifact.artifact_type}' created.",
                stage_run_id=stage_run.id,
                agent_id=stage_run.agent_id,
                semantics="reference",
                payload={
                    "artifact_id": artifact.id,
                    "artifact_type": artifact.artifact_type,
                    "artifact_version": artifact.artifact_version,
                    "checksum": artifact.checksum,
                },
            )
            if result.pause:
                yield _event(
                    "pipeline_paused",
                    stage.stage_id,
                    result.pause_reason or "Pipeline paused for human review.",
                    payload=result.pause_payload,
                )
                yield _human_review_event(stage, artifact, result.pause_reason, result.pause_payload, terminal=True)
                return
            yield _event(
                "stage_completed",
                stage.stage_id,
                f"Stage '{stage.name}' completed.",
                stage_run_id=stage_run.id,
                agent_id=stage_run.agent_id,
                semantics="snapshot",
                payload={"artifact_id": artifact.id, "artifact_type": artifact.artifact_type},
            )

        final_artifact = next(
            (item for item in reversed(list(artifacts.values())) if item.artifact_type == definition.final_artifact_type),
            None,
        )
        if final_artifact is None or final_artifact.content_json is None:
            raise StageExecutionError("Pipeline did not create its final artifact.", code="missing_final_artifact")
        yield _event(
            "pipeline_completed",
            "pipeline",
            "Pipeline completed.",
            semantics="snapshot",
            payload={"final_artifact_id": final_artifact.id},
        )
        yield TaskHandlerEvent(
            event_type="result_snapshot",
            stage="finalize",
            message="Final pipeline result is available.",
            payload={"artifact_id": final_artifact.id, "result": final_artifact.content_json},
            stream_semantics="snapshot",
            source={"type": "artifact", "id": final_artifact.id},
            structured_output=final_artifact.content_json,
            final_content=json.dumps(final_artifact.content_json, ensure_ascii=False),
            terminal_status="succeeded",
            outcome="success",
        )

    async def _stream_agent_stage(
        self,
        *,
        task: TaskEntity,
        run: TaskRunEntity,
        stage: StageDefinition,
        stage_run_id: str,
        attempt: int,
        stage_input: dict[str, Any],
    ) -> AsyncIterator[TaskHandlerEvent]:
        config = stage.agent_config
        assert config is not None
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, config.agent_id)
        if profile is None or not profile.enabled:
            raise StageExecutionError(f"Agent profile '{config.agent_id}' is unavailable.", code="agent_unavailable")
        excess_tools = set(profile.default_tools) - set(config.tools)
        if excess_tools:
            raise StageExecutionError(
                f"Agent profile '{profile.agent_id}' has tools outside the stage allowlist: {sorted(excess_tools)}.",
                code="tool_policy_violation",
            )

        session_id = _stage_session_id(run, stage, attempt)
        request = SchedulingChatRequest(
            message=_stage_message(task, stage, stage_input),
            user_id=task.user_id,
            session_id=session_id,
            stream=True,
            skill_package=config.skill_package,
            extra_tools=list(config.tools),
            extra_datasets=list(config.datasets),
        )
        final_content = ""
        service = SchedulingService(self.options, tenant_id=task.tenant_id)
        async for event in service.stream_chat(profile, request):
            payload = event.model_dump(exclude_none=True)
            if event.event == "metadata":
                await update_stage_run(
                    stage_run_id,
                    thread_id=event.thread_id,
                    session_id=event.session_id,
                )
                yield _event(
                    "agent_started",
                    stage.stage_id,
                    f"Agent '{profile.agent_id}' started.",
                    stage_run_id=stage_run_id,
                    agent_id=profile.agent_id,
                    payload={"thread_id": event.thread_id, "session_id": event.session_id},
                )
                continue
            if event.event == "final":
                final_content = str((event.data or {}).get("content") or "")
                parsed = parse_json_output(final_content)
                if not parsed.ok or not isinstance(parsed.structured, dict):
                    raise StageExecutionError(
                        f"Agent output is not valid JSON: {parsed.error}",
                        code="invalid_output",
                        retryable=config.output_policy == "repair_once",
                    )
                yield TaskHandlerEvent(
                    event_type="agent_completed",
                    stage=stage.stage_id,
                    message=f"Agent '{profile.agent_id}' completed.",
                    payload={"usage": (event.data or {}).get("usage")},
                    token_usage=(event.data or {}).get("usage"),
                    stage_run_id=stage_run_id,
                    agent_id=profile.agent_id,
                    source={"type": "agent", "id": profile.agent_id},
                    structured_output=parsed.structured,
                )
                continue

            data = event.data or {}
            for action in data.get("actions") or []:
                function = action.get("function") or {}
                call_id = str(action.get("id") or "")
                yield _event(
                    "tool_started",
                    stage.stage_id,
                    f"Tool '{function.get('name') or 'unknown'}' started.",
                    stage_run_id=stage_run_id,
                    agent_id=profile.agent_id,
                    tool_call_id=call_id,
                    payload={"tool": function.get("name"), "arguments": function.get("arguments")},
                    source={"type": "tool", "id": function.get("name") or "unknown"},
                )
            observation = data.get("observation")
            if isinstance(observation, dict):
                tool = observation.get("tool") or {}
                call_id = str(tool.get("id") or "") if isinstance(tool, dict) else ""
                error = observation.get("error")
                yield _event(
                    "tool_failed" if error else "tool_completed",
                    stage.stage_id,
                    "Tool failed." if error else "Tool completed.",
                    level="error" if error else "info",
                    stage_run_id=stage_run_id,
                    agent_id=profile.agent_id,
                    tool_call_id=call_id,
                    payload={"error": str(error or ""), "has_result": observation.get("result") is not None},
                    source={"type": "tool", "id": call_id or "tool"},
                )
            delta = _extract_delta(data)
            if delta:
                yield TaskHandlerEvent(
                    event_type="agent_delta",
                    stage=stage.stage_id,
                    message="Agent output chunk received.",
                    payload={},
                    delta=delta,
                    stage_run_id=stage_run_id,
                    agent_id=profile.agent_id,
                    stream_semantics="delta",
                    source={"type": "agent", "id": profile.agent_id},
                )


def _build_stage_input(
    task: TaskEntity,
    stage: StageDefinition,
    artifacts: dict[str, TaskArtifactEntity],
) -> tuple[dict[str, Any], list[TaskArtifactEntity]]:
    dependencies = [artifacts[item] for item in stage.depends_on if item in artifacts]
    if len(dependencies) != len(stage.depends_on):
        missing = [item for item in stage.depends_on if item not in artifacts]
        raise StageExecutionError(
            f"Stage '{stage.stage_id}' is missing dependency artifacts: {missing}.",
            code="missing_dependency_artifact",
        )
    if stage.input_adapter in {None, "pipeline_context"}:
        return {
            "task_input": task.input_payload_json or {},
            "artifacts": {item.artifact_type: item.content_json for item in dependencies},
        }, dependencies
    if stage.input_adapter == "task_input":
        return dict(task.input_payload_json or {}), dependencies
    if stage.input_adapter == "single_dependency":
        if len(dependencies) != 1 or dependencies[0].content_json is None:
            raise StageExecutionError("single_dependency requires exactly one JSON artifact.", code="invalid_input_adapter")
        return dict(dependencies[0].content_json), dependencies
    raise StageExecutionError(f"Unknown input adapter '{stage.input_adapter}'.", code="invalid_input_adapter")


async def _artifacts_by_stage(run_id: str) -> dict[str, TaskArtifactEntity]:
    stage_runs = {item.id: item for item in await list_stage_runs(run_id)}
    result: dict[str, TaskArtifactEntity] = {}
    for artifact in await list_artifacts(run_id=run_id):
        stage_run = stage_runs.get(artifact.stage_run_id)
        if stage_run is not None:
            result[stage_run.stage_id] = artifact
    return result


def _stage_message(task: TaskEntity, stage: StageDefinition, stage_input: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Execute one isolated TaskManager Pipeline stage.",
            f"Task type: {task.task_type}",
            f"Stage id: {stage.stage_id}",
            f"Stage name: {stage.name}",
            f"Required output schema: {stage.output_schema or 'JSON object'}",
            "Return exactly one valid JSON object and no Markdown outside it.",
            "Stage input:",
            json.dumps(stage_input, ensure_ascii=False, indent=2),
        ]
    )


def _stage_session_id(run: TaskRunEntity, stage: StageDefinition, attempt: int) -> str:
    config = stage.agent_config
    assert config is not None
    if config.session_policy == "reuse_previous_attempt":
        return f"{run.id}:{stage.stage_id}"
    if config.session_policy == "reuse_named_session":
        return f"pipeline:{run.pipeline_id}:{stage.stage_id}"
    return f"{run.id}:{stage.stage_id}:{attempt}"


async def _raise_if_cancelled(run_id: str) -> None:
    run = await get_run(run_id)
    if run is None:
        raise PipelineCancelled(f"Run '{run_id}' no longer exists.")
    if run.cancel_requested:
        raise PipelineCancelled("Run cancellation requested.")


def _can_retry(stage: StageDefinition, error: StageExecutionError) -> bool:
    retry_on = set(stage.retry_policy.retry_on)
    return error.retryable and (not retry_on or error.code in retry_on)


async def _mark_stage_failed(stage_run_id: str, started: float, error: StageExecutionError) -> None:
    await update_stage_run(
        stage_run_id,
        status="failed",
        finished_at=utc_now(),
        duration_ms=_duration_ms(started),
        error_code=error.code,
        error_message=str(error),
    )


def _duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _extract_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")


def _human_review_event(
    stage: StageDefinition,
    artifact: TaskArtifactEntity | None,
    reason: str,
    payload: dict[str, Any] | None = None,
    *,
    terminal: bool,
) -> TaskHandlerEvent:
    return TaskHandlerEvent(
        event_type="human_review_required",
        stage=stage.stage_id,
        message=reason or "Human review is required.",
        payload={**(payload or {}), "review_artifact_id": artifact.id if artifact else None},
        stage_run_id=artifact.stage_run_id if artifact else None,
        stream_semantics="status",
        source={"type": "pipeline", "id": stage.stage_id},
        terminal_status="waiting_human" if terminal else None,
        outcome="needs_human_review" if terminal else None,
    )


def _event(
    event_type: str,
    stage: str,
    message: str,
    *,
    payload: dict[str, Any] | None = None,
    level: str = "info",
    stage_run_id: str | None = None,
    agent_id: str | None = None,
    tool_call_id: str | None = None,
    semantics: str = "status",
    source: dict[str, Any] | None = None,
) -> TaskHandlerEvent:
    return TaskHandlerEvent(
        event_type=event_type,
        stage=stage,
        message=message,
        payload=payload or {},
        level=level,
        stage_run_id=stage_run_id,
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        stream_semantics=semantics,
        source=source or {"type": "pipeline", "id": stage},
    )
