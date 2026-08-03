from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from db.db_context import create_db_session
from sqlalchemy import func, or_
from sqlalchemy.exc import DBAPIError
from sqlmodel import select
from starlette.responses import StreamingResponse

from task_manager.models import (
    TaskEntity,
    TaskEventEntity,
    TaskRunEntity,
    TaskStageRunEntity,
)

from .auth import InternalAuthContext
from .errors import InternalObservabilityError, invalid_request
from .event_registry import contains_sensitive_material
from .models import utc_now
from .queries import TaskSecurityQueryService, task_event_to_dto
from .schemas import StreamReset

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store, private",
    "Referrer-Policy": "no-referrer",
    "Vary": "Authorization, X-Observability-Scope",
    "X-Accel-Buffering": "no",
}
TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "waiting_human"}


REPLAYABLE_PERSISTED_STREAM_SEMANTICS = frozenset({"status", "reference"})
TERMINAL_EVENT_TYPES = frozenset(
    {
        "task_succeeded",
        "task_failed",
        "task_cancelled",
        "human_review_required",
    }
)


@dataclass(slots=True)
class StreamPlan:
    run: TaskRunEntity
    task: TaskEntity
    request_id: str
    after_sequence: int
    max_connection_seconds: float = 30 * 60


@dataclass(slots=True)
class StreamProgress:
    run_id: str
    last_sequence: int = 0


class InternalSSEService:
    def __init__(
        self,
        queries: TaskSecurityQueryService,
        *,
        replay_event_limit: int = 1000,
        backlog_event_limit: int = 1000,
        backlog_byte_limit: int = 5 * 1024 * 1024,
        slow_client_seconds: float = 30.0,
        poll_seconds: float = 0.25,
        heartbeat_seconds: float = 15.0,
        max_connection_seconds: float = 30 * 60,
    ) -> None:
        self.queries = queries
        self.replay_event_limit = max(1, replay_event_limit)
        self.backlog_event_limit = max(1, backlog_event_limit)
        self.backlog_byte_limit = max(1024, backlog_byte_limit)
        self.slow_client_seconds = max(0.01, slow_client_seconds)
        self.poll_seconds = max(0.01, poll_seconds)
        self.heartbeat_seconds = max(self.poll_seconds, heartbeat_seconds)
        self.max_connection_seconds = max(
            self.heartbeat_seconds, max_connection_seconds
        )

    async def prepare(
        self,
        context: InternalAuthContext,
        *,
        run_id: str,
        last_event_id: str | None,
    ) -> StreamPlan:
        run, task = await self.queries.authorize_run(context, run_id)
        if last_event_id is not None and contains_sensitive_material(last_event_id):
            raise invalid_request(context.request_id)
        if run.task_id != task.id:
            raise InternalObservabilityError(
                503, "OBSERVABILITY_SOURCE_DATA_INVALID", context.request_id
            )
        if _is_archived(run):
            raise InternalObservabilityError(
                410, "EVENT_STREAM_RESET_REQUIRED", context.request_id
            )
        after_sequence = 0
        async with create_db_session() as session:
            ordered = select(TaskEventEntity).where(
                TaskEventEntity.run_id == run.id,
                TaskEventEntity.task_id == task.id,
            )
            earliest = (
                await session.exec(
                    ordered.order_by(
                        TaskEventEntity.sequence.asc(), TaskEventEntity.id.asc()
                    ).limit(1)
                )
            ).first()
            latest = (
                await session.exec(
                    ordered.order_by(
                        TaskEventEntity.sequence.desc(), TaskEventEntity.id.desc()
                    ).limit(1)
                )
            ).first()
            latest_sequence = latest.sequence if latest else 0
            if last_event_id:
                previous = await session.get(TaskEventEntity, last_event_id)
                if (
                    previous is None
                    or previous.run_id != run.id
                    or previous.task_id != task.id
                    or previous.tenant_id != task.tenant_id
                ):
                    raise InternalObservabilityError(
                        410, "EVENT_STREAM_RESET_REQUIRED", context.request_id
                    )
                after_sequence = previous.sequence
                history_count, history_min, history_max = (
                    await session.exec(
                        select(
                            func.count(TaskEventEntity.id),
                            func.min(TaskEventEntity.sequence),
                            func.max(TaskEventEntity.sequence),
                        )
                        .where(TaskEventEntity.run_id == run.id)
                        .where(TaskEventEntity.task_id == task.id)
                        .where(TaskEventEntity.tenant_id == task.tenant_id)
                        .where(TaskEventEntity.sequence <= after_sequence)
                    )
                ).one()
                non_replayable_checkpoint = (
                    await session.exec(
                        select(TaskEventEntity.id)
                        .where(TaskEventEntity.run_id == run.id)
                        .where(TaskEventEntity.task_id == task.id)
                        .where(TaskEventEntity.tenant_id == task.tenant_id)
                        .where(TaskEventEntity.sequence <= after_sequence)
                        .where(
                            or_(
                                TaskEventEntity.stream_semantics.is_(None),
                                TaskEventEntity.stream_semantics.notin_(
                                    tuple(REPLAYABLE_PERSISTED_STREAM_SEMANTICS)
                                ),
                            )
                        )
                        .limit(1)
                    )
                ).first()
                if (
                    history_count != after_sequence
                    or history_min != 1
                    or history_max != after_sequence
                    or non_replayable_checkpoint is not None
                ):
                    raise InternalObservabilityError(
                        410, "EVENT_STREAM_RESET_REQUIRED", context.request_id
                    )
                next_row = (
                    await session.exec(
                        ordered.where(TaskEventEntity.sequence > after_sequence)
                        .order_by(
                            TaskEventEntity.sequence.asc(), TaskEventEntity.id.asc()
                        )
                        .limit(1)
                    )
                ).first()
                if next_row is not None and next_row.sequence != after_sequence + 1:
                    raise InternalObservabilityError(
                        410, "EVENT_STREAM_RESET_REQUIRED", context.request_id
                    )
                if latest_sequence - after_sequence > self.replay_event_limit:
                    raise InternalObservabilityError(
                        409,
                        "EVENT_STREAM_REPLAY_GAP",
                        context.request_id,
                        retryable=True,
                    )
            else:
                if earliest is not None and earliest.sequence != 1:
                    raise InternalObservabilityError(
                        410, "EVENT_STREAM_RESET_REQUIRED", context.request_id
                    )
                if latest_sequence > self.replay_event_limit:
                    raise InternalObservabilityError(
                        409,
                        "EVENT_STREAM_REPLAY_GAP",
                        context.request_id,
                        retryable=True,
                    )
        scoped_max_seconds = float(
            context.max_stream_seconds or self.max_connection_seconds
        )
        return StreamPlan(
            run=run,
            task=task,
            request_id=context.request_id,
            after_sequence=after_sequence,
            max_connection_seconds=min(self.max_connection_seconds, scoped_max_seconds),
        )

    def response(self, plan: StreamPlan) -> StreamingResponse:
        progress = StreamProgress(run_id=plan.run.id, last_sequence=plan.after_sequence)
        return SlowClientProtectedStreamingResponse(
            self.stream(plan, progress),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
            progress=progress,
            slow_client_seconds=self.slow_client_seconds,
        )

    async def stream(
        self,
        plan: StreamPlan,
        progress: StreamProgress | None = None,
    ) -> AsyncIterator[str]:
        progress = progress or StreamProgress(plan.run.id, plan.after_sequence)
        started = monotonic()
        heartbeat_at = started + self.heartbeat_seconds
        last_sequence = plan.after_sequence
        while True:
            try:
                run = await self._get_run(plan.run.id)
            except (InternalObservabilityError, DBAPIError, TimeoutError) as exc:
                yield _stream_source_failure_reset(
                    plan,
                    last_sequence,
                    plan.run,
                    exc,
                )
                return
            if run is None or run.task_id != plan.task.id or _is_archived(run):
                yield _reset_required(plan.run.id, last_sequence)
                return

            try:
                pending = await self._events_after(
                    plan.run.id, plan.task.id, last_sequence
                )
            except (InternalObservabilityError, DBAPIError, TimeoutError) as exc:
                yield _stream_source_failure_reset(
                    plan,
                    last_sequence,
                    run,
                    exc,
                )
                return
            if pending:
                expected = last_sequence + 1
                if pending[0].sequence != expected:
                    yield _stream_reset_required(
                        plan.run.id,
                        last_sequence,
                        pending,
                        run_status=run.status,
                        reason="LIVE_SEQUENCE_GAP",
                        latest_sequence=run.next_event_sequence,
                    )
                    return
                if any(
                    row.sequence != expected + offset
                    for offset, row in enumerate(pending)
                ):
                    yield _stream_reset_required(
                        plan.run.id,
                        last_sequence,
                        pending,
                        run_status=run.status,
                        reason="LIVE_SEQUENCE_GAP",
                        latest_sequence=run.next_event_sequence,
                    )
                    return
                if any(
                    row.stream_semantics not in REPLAYABLE_PERSISTED_STREAM_SEMANTICS
                    for row in pending
                ):
                    yield _stream_reset_required(
                        plan.run.id,
                        last_sequence,
                        pending,
                        run_status=run.status,
                        reason="NON_REPLAYABLE_HISTORY",
                        latest_sequence=run.next_event_sequence,
                    )
                    return
                encoded: list[str] = []
                byte_count = 0
                try:
                    for row in pending:
                        if (
                            row.sequence != expected
                            or row.task_id != plan.task.id
                            or row.run_id != plan.run.id
                            or row.tenant_id != plan.task.tenant_id
                        ):
                            raise ValueError
                        block = _task_event_block(row, plan.task, plan.request_id)
                        byte_count += len(block.encode("utf-8"))
                        encoded.append(block)
                        expected += 1
                except (InternalObservabilityError, ValueError):
                    yield _reset_required(plan.run.id, last_sequence)
                    return
                if (
                    len(encoded) > self.backlog_event_limit
                    or byte_count > self.backlog_byte_limit
                ):
                    yield _reset_required(plan.run.id, last_sequence)
                    return
                for row, block in zip(pending, encoded, strict=True):
                    last_sequence = row.sequence
                    progress.last_sequence = last_sequence
                    yield block
                heartbeat_at = monotonic() + self.heartbeat_seconds
                continue

            if run.status in TERMINAL_RUN_STATUSES:
                return
            now = monotonic()
            if now - started >= plan.max_connection_seconds:
                yield _event_block(
                    event="reconnect",
                    data={"runId": plan.run.id, "lastSequence": last_sequence},
                )
                return
            if now >= heartbeat_at:
                yield _event_block(
                    event="heartbeat",
                    data={
                        "runId": plan.run.id,
                        "sequence": last_sequence,
                        "occurredAt": utc_now().isoformat() + "Z",
                    },
                )
                heartbeat_at = now + self.heartbeat_seconds
            await asyncio.sleep(self.poll_seconds)

    async def _events_after(
        self,
        run_id: str,
        task_id: str,
        sequence: int,
    ) -> list[TaskEventEntity]:
        limit = self.backlog_event_limit + 1
        async with create_db_session() as session:
            rows = list(
                (
                    await session.exec(
                        select(TaskEventEntity)
                        .where(TaskEventEntity.run_id == run_id)
                        .where(TaskEventEntity.task_id == task_id)
                        .where(TaskEventEntity.sequence > sequence)
                        .order_by(
                            TaskEventEntity.sequence.asc(),
                            TaskEventEntity.id.asc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )
            stage_ids = {
                row.stage_run_id for row in rows if row.stage_run_id is not None
            }
            stages: dict[str, TaskStageRunEntity] = {}
            if stage_ids:
                stage_rows = list(
                    (
                        await session.exec(
                            select(TaskStageRunEntity).where(
                                TaskStageRunEntity.id.in_(stage_ids)
                            )
                        )
                    ).all()
                )
                stages = {row.id: row for row in stage_rows}
            for row in rows:
                if row.stage_run_id is not None:
                    stage = stages.get(row.stage_run_id)
                    if (
                        stage is None
                        or stage.task_id != task_id
                        or stage.run_id != run_id
                    ):
                        raise InternalObservabilityError(
                            503,
                            "OBSERVABILITY_SOURCE_DATA_INVALID",
                            "req_stream",
                        )
            return rows

    @staticmethod
    async def _get_run(run_id: str) -> TaskRunEntity | None:
        async with create_db_session() as session:
            return await session.get(TaskRunEntity, run_id)


class SlowClientProtectedStreamingResponse(StreamingResponse):
    """Bound each socket send and close slow clients with reset-required."""

    def __init__(
        self,
        *args,
        progress: StreamProgress,
        slow_client_seconds: float,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._progress = progress
        self._slow_client_seconds = slow_client_seconds

    async def stream_response(self, send: Callable[[dict], Awaitable[None]]) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        timed_out = False
        async for chunk in self.body_iterator:
            if not isinstance(chunk, (bytes, memoryview)):
                chunk = chunk.encode(self.charset)
            try:
                await asyncio.wait_for(
                    send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    ),
                    timeout=self._slow_client_seconds,
                )
            except TimeoutError:
                timed_out = True
                break
        if timed_out:
            reset = _reset_required(
                self._progress.run_id, self._progress.last_sequence
            ).encode(self.charset)
            try:
                await asyncio.wait_for(
                    send(
                        {"type": "http.response.body", "body": reset, "more_body": True}
                    ),
                    timeout=self._slow_client_seconds,
                )
            except TimeoutError:
                pass
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:  # noqa: BLE001 -- final ASGI close is best-effort
            return


def _task_event_block(
    row: TaskEventEntity,
    task: TaskEntity,
    request_id: str,
) -> str:
    event = task_event_to_dto(row, task, request_id)
    return _event_block(
        event="task-event",
        event_id=row.id,
        data={
            "sequence": row.sequence,
            "occurredAt": event.occurred_at.isoformat(),
            "event": event.model_dump(by_alias=True, mode="json"),
        },
    )


def _reset_required(run_id: str, last_available_sequence: int) -> str:
    payload = StreamReset(
        code="EVENT_STREAM_RESET_REQUIRED",
        run_id=run_id,
        last_available_sequence=max(0, last_available_sequence),
    )
    return _event_block(
        event="reset-required",
        data=payload.model_dump(by_alias=True, mode="json"),
    )


def _stream_reset_required(
    run_id: str,
    last_sequence: int,
    pending: list[TaskEventEntity],
    *,
    run_status: str,
    reason: str,
    latest_sequence: int,
) -> str:
    terminal_event = next(
        (
            {
                "eventId": row.id,
                "eventType": row.event_type,
                "sequence": row.sequence,
            }
            for row in reversed(pending)
            if row.event_type in TERMINAL_EVENT_TYPES
        ),
        None,
    )
    pending_sequences = [
        row.sequence
        for row in pending
        if isinstance(row.sequence, int)
        and not isinstance(row.sequence, bool)
        and row.sequence > 0
    ]
    if isinstance(latest_sequence, bool) or not isinstance(latest_sequence, int):
        latest_sequence = 0
    data = {
        "code": "EVENT_STREAM_RESET_REQUIRED",
        "runId": run_id,
        "reason": reason,
        "lastReplayableSequence": max(0, last_sequence),
        "lastAvailableSequence": max(
            [last_sequence, latest_sequence, *pending_sequences]
        ),
        "runStatus": run_status,
        "terminal": run_status in TERMINAL_RUN_STATUSES or terminal_event is not None,
    }
    if terminal_event is not None:
        data["terminalEvent"] = terminal_event
    return _event_block(event="reset-required", data=data)


def _stream_source_failure_reset(
    plan: StreamPlan,
    last_sequence: int,
    run: TaskRunEntity,
    error: Exception,
) -> str:
    if (
        isinstance(error, InternalObservabilityError)
        and error.code == "OBSERVABILITY_SOURCE_DATA_INVALID"
    ):
        reason = "SOURCE_DATA_INVALID"
    elif isinstance(error, TimeoutError):
        reason = "SOURCE_TIMEOUT"
    else:
        reason = "SOURCE_UNAVAILABLE"
    return _stream_reset_required(
        plan.run.id,
        last_sequence,
        [],
        run_status=run.status,
        reason=reason,
        latest_sequence=run.next_event_sequence,
    )


def _event_block(
    *,
    event: str,
    data: dict,
    event_id: str | None = None,
) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


def _is_archived(run: TaskRunEntity) -> bool:
    metadata = run.metadata_json
    if not isinstance(metadata, dict):
        return True
    return run.status == "archived" or bool(
        metadata.get("archived_at") or metadata.get("archivedAt")
    )
