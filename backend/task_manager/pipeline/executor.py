from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService
from service.conversation import LlmRuntime
from skill import SkillManager

from task_manager.handlers.batch_item_scheduler import BatchItemSchedulerHandler
from task_manager.handlers.base import TaskExecutionContext, TaskHandlerEvent
from task_manager.artifact_service import TaskArtifactPublisher
from task_manager import item_store
from task_manager.models import TaskArtifactEntity, TaskEntity, TaskRunEntity, utc_now
from task_manager.output_parser import parse_json_output
from task_manager.payload_schemas import get_stage_json_schema, validate_stage_payload
from task_manager.registry import TaskType
from task_manager.result_sink import (
    RequiredResultSinkError,
    ResultSinkRejectedError,
    deliver_task_result,
    is_required_result_sink,
)
from tool.artifacts import extract_artifacts

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
        context: TaskExecutionContext,
        run: TaskRunEntity,
        definition: PipelineDefinition,
    ) -> AsyncIterator[TaskHandlerEvent]:
        task = context.task
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

        yield _event(
            "pipeline_started",
            "pipeline",
            "Pipeline started.",
            payload={"pipeline_id": definition.pipeline_id, "version": definition.version},
        )

        stage_positions = {stage.stage_id: index for index, stage in enumerate(ordered, start=1)}
        pending = {stage.stage_id: stage for stage in ordered if stage.stage_id not in artifacts}
        resolved = set(artifacts)
        if resume_from:
            before_resume = True
            for stage in ordered:
                if stage.stage_id == resume_from:
                    before_resume = False
                elif before_resume:
                    pending.pop(stage.stage_id, None)
                    resolved.add(stage.stage_id)

        while pending:
            await _raise_if_cancelled(run.id)
            ready = [
                stage for stage in ordered
                if stage.stage_id in pending and set(stage.depends_on) <= resolved
            ]
            if not ready:
                raise StageExecutionError("Pipeline has no runnable stage.", code="pipeline_stalled")
            wave = ready[: definition.max_parallelism]
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            async def pump(stage: StageDefinition) -> None:
                paused = False
                try:
                    async for event in self._stream_stage(
                        context=context,
                        run=run,
                        definition=definition,
                        stage=stage,
                        stage_index=stage_positions[stage.stage_id],
                        artifacts=artifacts,
                        attempt_offsets=attempt_offsets,
                    ):
                        paused = paused or event.terminal_status == "waiting_human"
                        await queue.put(("event", event))
                    await queue.put(("done", (stage.stage_id, paused)))
                except BaseException as exc:
                    await queue.put(("error", (stage.stage_id, exc)))

            workers = [asyncio.create_task(pump(stage)) for stage in wave]
            finished = 0
            wave_error: BaseException | None = None
            paused = False
            while finished < len(workers):
                kind, value = await queue.get()
                if kind == "event":
                    yield value
                elif kind == "done":
                    stage_id, stage_paused = value
                    pending.pop(stage_id, None)
                    resolved.add(stage_id)
                    paused = paused or stage_paused
                    finished += 1
                else:
                    stage_id, wave_error = value
                    pending.pop(stage_id, None)
                    finished += 1
                    break
            if wave_error is not None or paused:
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if wave_error is not None:
                    raise wave_error
                return
            await asyncio.gather(*workers)

        final_artifact = next(
            (item for item in reversed(list(artifacts.values())) if item.artifact_type == definition.final_artifact_type),
            None,
        )
        if final_artifact is None or final_artifact.content_json is None:
            raise StageExecutionError("Pipeline did not create its final artifact.", code="missing_final_artifact")
        await deliver_task_result(context.task, context.task_type, final_artifact.content_json)
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

    async def _stream_stage(
        self,
        *,
        context: TaskExecutionContext,
        run: TaskRunEntity,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_index: int,
        artifacts: dict[str, TaskArtifactEntity],
        attempt_offsets: dict[str, int],
    ) -> AsyncIterator[TaskHandlerEvent]:
        task = context.task
        await update_run(run.id, current_stage_id=stage.stage_id, status="running")
        stage_input, input_artifacts = _build_stage_input(task, stage, artifacts)
        try:
            stage_input = validate_stage_payload(stage.input_schema, stage_input)
        except ValueError as exc:
            raise StageExecutionError(str(exc), code="invalid_stage_input") from exc

        result: StageServiceResult | None = None
        last_error: StageExecutionError | None = None
        stage_run = None
        started = time.perf_counter()
        required_sink = is_required_result_sink(task.task_type)
        for local_attempt in range(1, stage.retry_policy.max_attempts + 1):
            attempt = attempt_offsets.get(stage.stage_id, 0) + local_attempt
            result = None
            await _raise_if_cancelled(run.id)
            agent_id = (
                stage.agent_config.agent_id if stage.agent_config
                else stage.batch_config.agent_id if stage.batch_config
                else None
            )
            stage_run = await create_stage_run(
                task_id=task.id,
                run_id=run.id,
                stage_id=stage.stage_id,
                stage_type=stage.stage_type,
                attempt=attempt,
                agent_id=agent_id,
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
                        stage_events = self._stream_agent_stage(
                            context=context, task=task, run=run, stage=stage,
                            stage_run_id=stage_run.id, attempt=attempt, stage_input=stage_input,
                            retry_feedback=str(last_error) if last_error is not None else None,
                        )
                    elif stage.stage_type == "direct_model":
                        stage_events = self._stream_direct_model_stage(
                            context=context, task=task, stage=stage,
                            stage_run_id=stage_run.id, stage_input=stage_input,
                        )
                    elif stage.stage_type == "batch":
                        stage_events = self._stream_batch_stage(
                            context=context,
                            stage=stage,
                            stage_run_id=stage_run.id,
                            stage_input=stage_input,
                        )
                    else:
                        stage_events = None

                    if stage_events is not None:
                        async for item in stage_events:
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
                result.output = validate_stage_payload(stage.output_schema, result.output)
                if required_sink:
                    try:
                        await deliver_task_result(
                            task,
                            context.task_type,
                            result.output,
                            stage_id=stage.stage_id,
                        )
                    except RequiredResultSinkError as exc:
                        retryable = isinstance(exc.__cause__, ResultSinkRejectedError)
                        raise StageExecutionError(
                            str(exc),
                            code="required_result_sink_failed",
                            retryable=retryable,
                        ) from exc
                break
            except TimeoutError:
                last_error = StageExecutionError("Stage timed out.", code="timeout", retryable=True)
            except StageExecutionError as exc:
                last_error = exc
            except ValueError as exc:
                last_error = StageExecutionError(str(exc), code="invalid_output", retryable=True)
            except PipelineCancelled:
                await update_stage_run(
                    stage_run.id, status="cancelled", finished_at=utc_now(), duration_ms=_duration_ms(started)
                )
                raise
            except Exception as exc:
                last_error = StageExecutionError(str(exc), code=exc.__class__.__name__, retryable=False)
            result = None
            await _mark_stage_failed(stage_run.id, started, last_error)
            yield _event(
                "stage_failed", stage.stage_id, str(last_error), level="error",
                stage_run_id=stage_run.id, agent_id=stage_run.agent_id,
                payload={"attempt": attempt, "error_code": last_error.code},
            )
            if local_attempt < stage.retry_policy.max_attempts and _can_retry(stage, last_error):
                yield _event(
                    "stage_retrying", stage.stage_id, f"Retrying stage '{stage.name}'.",
                    stage_run_id=stage_run.id,
                    payload={"next_attempt": attempt + 1, "backoff_seconds": stage.retry_policy.backoff_seconds},
                )
                if stage.retry_policy.backoff_seconds:
                    await asyncio.sleep(stage.retry_policy.backoff_seconds)
                continue
            break

        if result is None:
            assert last_error is not None
            try:
                await deliver_task_result(
                    task,
                    context.task_type,
                    None,
                    stage_id=stage.stage_id,
                    status="failed",
                    error_message=str(last_error),
                    error_code=last_error.code,
                    retryable=last_error.retryable,
                )
            except Exception as sink_error:
                yield _event(
                    "stage_result_sink_failed",
                    stage.stage_id,
                    str(sink_error),
                    level="warning",
                )
            if stage.failure_policy in {"continue_with_warning", "skip_stage"}:
                yield _event(
                    "stage_skipped", stage.stage_id, f"Stage skipped after failure: {last_error}",
                    level="warning",
                    payload={"failure_policy": stage.failure_policy, "error_message": str(last_error)},
                )
                return
            if stage.failure_policy == "require_human":
                yield _event(
                    "pipeline_paused", stage.stage_id, "Pipeline paused after a stage failure.",
                    level="warning", payload={"reason": str(last_error)},
                )
                yield _human_review_event(stage, None, str(last_error), terminal=True)
                return
            raise last_error

        assert stage_run is not None
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
        if not required_sink:
            try:
                await deliver_task_result(
                    task,
                    context.task_type,
                    result.output,
                    stage_id=stage.stage_id,
                )
            except Exception as sink_error:
                yield _event(
                    "stage_result_sink_failed",
                    stage.stage_id,
                    str(sink_error),
                    level="warning",
                    stage_run_id=stage_run.id,
                )
        yield _event(
            "artifact_created", stage.stage_id, f"Artifact '{artifact.artifact_type}' created.",
            stage_run_id=stage_run.id, agent_id=stage_run.agent_id, semantics="reference",
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "artifact_version": artifact.artifact_version,
                "checksum": artifact.checksum,
            },
        )
        if result.pause:
            yield _event(
                "pipeline_paused", stage.stage_id,
                result.pause_reason or "Pipeline paused for human review.", payload=result.pause_payload,
            )
            yield _human_review_event(stage, artifact, result.pause_reason, result.pause_payload, terminal=True)
            return
        yield _event(
            "stage_completed", stage.stage_id, f"Stage '{stage.name}' completed.",
            stage_run_id=stage_run.id, agent_id=stage_run.agent_id, semantics="snapshot",
            payload={"artifact_id": artifact.id, "artifact_type": artifact.artifact_type},
        )

    async def _stream_direct_model_stage(
        self,
        *,
        context: TaskExecutionContext,
        task: TaskEntity,
        stage: StageDefinition,
        stage_run_id: str,
        stage_input: dict[str, Any],
    ) -> AsyncIterator[TaskHandlerEvent]:
        config = stage.agent_config
        assert config is not None
        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, config.agent_id)
        if profile is None or not profile.enabled:
            raise StageExecutionError(f"Agent profile '{config.agent_id}' is unavailable.", code="agent_unavailable")
        if profile.default_tools or profile.default_datasets or config.tools or config.datasets:
            raise StageExecutionError(
                f"Direct model profile '{profile.agent_id}' must not use tools or datasets.",
                code="direct_model_tool_policy_violation",
            )
        skill_context = await SkillManager(tenant_id=task.tenant_id).create_context(config.skill_package)
        system_prompt = "\n\n".join(
            item for item in (profile.system_prompt, skill_context.task_prompt) if item
        )
        yield _event(
            "direct_model_started",
            stage.stage_id,
            f"Direct model '{profile.agent_id}' started.",
            stage_run_id=stage_run_id,
            agent_id=profile.agent_id,
            payload={"model_id": profile.model_id, "skill_package": config.skill_package},
            source={"type": "agent", "id": profile.agent_id},
        )
        content = await LlmRuntime(tenant_id=task.tenant_id).complete(
            messages=[{"role": "user", "content": _stage_message(task, stage, stage_input)}],
            model_id=profile.model_id,
            system_prompt=system_prompt,
        )
        parsed = parse_json_output(content)
        if not parsed.ok or not isinstance(parsed.structured, dict):
            raise StageExecutionError(
                f"Direct model output is not valid JSON: {parsed.error}",
                code="invalid_output",
                retryable=config.output_policy == "repair_once",
            )
        yield TaskHandlerEvent(
            event_type="direct_model_completed",
            stage=stage.stage_id,
            message=f"Direct model '{profile.agent_id}' completed.",
            stage_run_id=stage_run_id,
            agent_id=profile.agent_id,
            source={"type": "agent", "id": profile.agent_id},
            structured_output=parsed.structured,
        )

    async def _stream_batch_stage(
        self,
        *,
        context: TaskExecutionContext,
        stage: StageDefinition,
        stage_run_id: str,
        stage_input: dict[str, Any],
    ) -> AsyncIterator[TaskHandlerEvent]:
        config = stage.batch_config
        assert config is not None
        raw_items = (context.task.input_payload_json or {}).get(config.item_source)
        if not isinstance(raw_items, list):
            raise StageExecutionError(
                f"Batch stage '{stage.stage_id}' item source '{config.item_source}' is missing.",
                code="invalid_stage_items",
            )
        if not all(isinstance(item, dict) for item in raw_items):
            raise StageExecutionError(
                f"Batch stage '{stage.stage_id}' items must be objects.",
                code="invalid_stage_items",
            )
        stage_item_type = f"pipeline:{stage.stage_id}"
        await item_store.ensure_stage_items(
            context.task.id,
            item_type=stage_item_type,
            raw_items=raw_items,
        )
        # SQLModel table instances cannot be safely mutated after model_copy():
        # SQLAlchemy's copied attribute state still points at the original owner.
        # Build a fresh transient task view for this stage instead.
        stage_task = TaskEntity(**context.task.model_dump())
        stage_task.agent_id = config.agent_id
        stage_task.input_payload_json = {
            key: value
            for key, value in (context.task.input_payload_json or {}).items()
            if not key.endswith("_items")
        }
        stage_task.input_payload_json["items"] = raw_items
        batch_definition = TaskType(
            task_type=context.task.task_type,
            name=stage.name,
            handler="batch_item_scheduler",
            default_agent_id=config.agent_id,
            default_skill_package=config.skill_package,
            default_tools=list(config.tools),
            default_datasets=list(config.datasets),
            output_schema_name=stage.output_schema,
            item_output_schema_name=config.item_output_schema,
        )
        batch_context = TaskExecutionContext(
            task=stage_task,
            task_type=batch_definition,
            memory_view=context.memory_view,
            runtime_context=context.runtime_context,
        )
        structured: dict[str, Any] | None = None
        async for event in BatchItemSchedulerHandler(self.options).stream(
            context=batch_context,
            item_type=stage_item_type,
        ):
            if event.final_content:
                parsed = parse_json_output(event.final_content)
                if parsed.ok and isinstance(parsed.structured, dict):
                    structured = parsed.structured
            event.stage = stage.stage_id
            event.stage_run_id = event.stage_run_id or stage_run_id
            yield event
        if structured is None:
            raise StageExecutionError("Batch stage produced no structured output.", code="empty_stage_output")
        yield TaskHandlerEvent(
            event_type="batch_stage_completed",
            stage=stage.stage_id,
            message=f"Batch stage '{stage.name}' completed.",
            stage_run_id=stage_run_id,
            agent_id=config.agent_id,
            source={"type": "agent", "id": config.agent_id},
            structured_output=structured,
        )

    async def _stream_agent_stage(
        self,
        *,
        context: TaskExecutionContext,
        task: TaskEntity,
        run: TaskRunEntity,
        stage: StageDefinition,
        stage_run_id: str,
        attempt: int,
        stage_input: dict[str, Any],
        retry_feedback: str | None = None,
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
        stage_message = _stage_message(
            task,
            stage,
            stage_input,
            retry_feedback=retry_feedback,
        )

        request = SchedulingChatRequest(
            message=stage_message,
            user_id=task.user_id,
            session_id=session_id,
            stream=True,
            skill_package=config.skill_package,
            extra_tools=list(config.tools),
            extra_datasets=list(config.datasets),
        )
        final_content = ""
        service = SchedulingService(self.options, tenant_id=task.tenant_id)
        artifact_publisher = TaskArtifactPublisher(
            root=self.options.local_python_artifact_dir,
            task_id=task.id,
            run_id=run.id,
            stage_run_id=stage_run_id,
        )
        async for event in service.stream_chat(
            profile,
            request,
            runtime_context=context.runtime_context,
            artifact_publisher=artifact_publisher,
        ):
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
                    payload={
                        "thread_id": event.thread_id,
                        "session_id": event.session_id,
                        "task_memory_version": context.memory_view.version,
                    },
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
                    payload={
                        "error": str(error or ""),
                        "has_result": observation.get("result") is not None,
                        "artifacts": extract_artifacts(observation.get("result")),
                    },
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
    if len(dependencies) != len(stage.depends_on) and stage.stage_type != "finalizer":
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


def _stage_message(
    task: TaskEntity,
    stage: StageDefinition,
    stage_input: dict[str, Any],
    *,
    retry_feedback: str | None = None,
) -> str:
    output_schema = get_stage_json_schema(stage.output_schema)
    lines = [
        "Execute one isolated TaskManager Pipeline stage.",
        f"Task type: {task.task_type}",
        f"Stage id: {stage.stage_id}",
        f"Stage name: {stage.name}",
        f"Required output schema name: {stage.output_schema or 'JSON object'}",
        "Required output JSON Schema:",
        json.dumps(
            output_schema or {"type": "object"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "Return exactly one valid JSON object and no Markdown outside it.",
        "The final answer must start with '{' and end with '}'; do not narrate analysis before it.",
        "Stage input:",
        json.dumps(stage_input, ensure_ascii=False, indent=2),
    ]
    if retry_feedback:
        lines.extend(
            [
                "Previous attempt rejection:",
                retry_feedback[:2000],
                "Correct that rejection and return a new complete JSON object.",
            ]
        )
    return "\n".join(lines)


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
