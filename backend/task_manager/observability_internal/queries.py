from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from db.db_context import create_db_session
from pydantic import ValidationError
from sqlalchemy import and_, desc, or_
from sqlmodel import select

from task_manager.models import (
    TaskEntity,
    TaskEventEntity,
    TaskRunEntity,
    TaskStageRunEntity,
)

from .auth import InternalAuthContext
from .capabilities import canonical_hash
from .errors import (
    InternalObservabilityError,
    invalid_request,
    invalid_source,
    invisible,
)
from .event_registry import (
    RegistryValidationError,
    contains_sensitive_material,
    sanitize_task_event_source,
)
from .models import QuerySnapshotEntity, SecurityAuditEventEntity, utc_now
from .redaction import metadata_fields
from .schemas import (
    Actor,
    InternalRun,
    InternalSecurityEvent,
    InternalSecurityEventList,
    InternalStage,
    InternalStageList,
    InternalTask,
    InternalTaskEvent,
    InternalTaskEventList,
    InternalTaskList,
    PageInfo,
    Subject,
    TRACE_ID_PATTERN,
)
from .security import SecurityAuditService
from .snapshots import SnapshotStore

T = TypeVar("T")
SCHEMA_VERSION_PATTERN = re.compile(r"^(?P<major>[1-9][0-9]*)(?:\.0)?$")


@dataclass(slots=True)
class PageRequest:
    high_watermark: str | None
    cursor: str | None
    retry_token: str | None
    limit: int


@dataclass(slots=True)
class FrozenPage:
    row: QuerySnapshotEntity
    items: list[dict[str, Any]]
    position: int
    next_position: int
    has_more: bool


class TaskSecurityQueryService:
    def __init__(
        self, snapshots: SnapshotStore, security: SecurityAuditService
    ) -> None:
        self.snapshots = snapshots
        self.security = security

    async def list_tasks(
        self,
        context: InternalAuthContext,
        *,
        from_at: datetime,
        to_at: datetime,
        snapshot_to: datetime,
        page: PageRequest,
        feature_code: str | None = None,
        status: str | None = None,
        error_code: str | None = None,
    ) -> InternalTaskList:
        from_at, to_at, snapshot_to = _validate_window(
            from_at, to_at, snapshot_to, context.request_id
        )
        feature_code = _optional_filter(feature_code, maximum=120, context=context)
        status = _optional_filter(status, maximum=32, context=context)
        error_code = _optional_filter(error_code, maximum=120, context=context)
        filters = {
            "from": from_at.isoformat(),
            "to": to_at.isoformat(),
            "snapshotTo": snapshot_to.isoformat(),
            "featureCode": feature_code,
            "status": status,
            "errorCode": error_code,
        }

        async def load():
            async with create_db_session() as session:
                statement = (
                    select(TaskEntity, TaskRunEntity)
                    .join(
                        TaskRunEntity,
                        TaskRunEntity.id == TaskEntity.current_run_id,
                        isouter=True,
                    )
                    .where(TaskEntity.created_at >= from_at)
                    .where(TaskEntity.created_at <= to_at)
                )
                statement = _scope_statement(statement, TaskEntity.tenant_id, context)
                if feature_code is not None:
                    statement = statement.where(TaskEntity.task_type == feature_code)
                if status is not None:
                    statement = statement.where(TaskEntity.status == status)
                if error_code is not None:
                    statement = statement.where(
                        or_(
                            TaskEntity.error_payload_json["code"].as_string()
                            == error_code,
                            TaskEntity.error_payload_json["error_code"].as_string()
                            == error_code,
                            TaskEntity.error_payload_json["type"].as_string()
                            == error_code,
                        )
                    )
                row_pairs = list(
                    (
                        await session.exec(
                            statement.order_by(
                                desc(TaskEntity.created_at), desc(TaskEntity.id)
                            ).limit(self.snapshots.max_items + 1)
                        )
                    ).all()
                )
                data_as_of = _utc_value(utc_now())
                if len(row_pairs) > self.snapshots.max_items:
                    raise _snapshot_capacity(context.request_id)
                rows = [task for task, _run in row_pairs]
                items: list[dict[str, Any]] = []
                watermark_row = max(
                    rows,
                    key=lambda item: (_utc_value(item.updated_at), item.id),
                    default=None,
                )
                for row, run in row_pairs:
                    if row.current_run_id is not None and run is None:
                        raise invalid_source(context.request_id)
                    if run is not None and run.task_id != row.id:
                        raise invalid_source(context.request_id)
                    items.append(
                        _task_dto(row, run, data_as_of, context.request_id).model_dump(
                            by_alias=True, mode="json"
                        )
                    )
                return (
                    items,
                    watermark_row.updated_at if watermark_row is not None else None,
                    None,
                    watermark_row.id if watermark_row is not None else None,
                )

        frozen = await self._materialized_page(
            context,
            resource_kind="tasks",
            snapshot_mode="MATERIALIZED_RESULT_SET",
            filters=filters,
            snapshot_to=snapshot_to,
            page=page,
            loader=load,
        )
        return _validate_task_list(
            frozen, page.limit, self.snapshots, context.request_id
        )

    async def get_task(
        self, context: InternalAuthContext, task_id: str
    ) -> InternalTask:
        task_id = _required_identifier(
            task_id, maximum=80, request_id=context.request_id
        )
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise invisible(context.request_id, exists=False)
            if not context.permits_tenant(task.tenant_id):
                raise invisible(context.request_id, exists=True)
            run = None
            if task.current_run_id:
                run = await session.get(TaskRunEntity, task.current_run_id)
                if run is None or run.task_id != task.id:
                    raise invalid_source(context.request_id)
        return _task_dto(task, run, _utc_value(utc_now()), context.request_id)

    async def list_task_timeline(
        self,
        context: InternalAuthContext,
        *,
        task_id: str,
        snapshot_to: datetime,
        page: PageRequest,
    ) -> InternalTaskEventList:
        task = await self._authorized_task(context, task_id)
        snapshot_to = _validate_snapshot_time(snapshot_to, context.request_id)
        filters = {"taskId": task.id, "snapshotTo": snapshot_to.isoformat()}

        async def load():
            async with create_db_session() as session:
                rows = list(
                    (
                        await session.exec(
                            select(TaskEventEntity)
                            .where(TaskEventEntity.task_id == task.id)
                            .where(TaskEventEntity.ingested_at <= snapshot_to)
                            .order_by(
                                TaskEventEntity.sequence.asc(), TaskEventEntity.id.asc()
                            )
                            .limit(self.snapshots.max_items + 1)
                        )
                    ).all()
                )
                await _validate_event_relationships(
                    session, rows, {task.id: task}, context.request_id
                )
            return _freeze_events(
                rows, {task.id: task}, self.snapshots.max_items, context.request_id
            )

        frozen = await self._materialized_page(
            context,
            resource_kind="task-timeline",
            snapshot_mode="APPEND_ONLY_HIGH_WATERMARK",
            filters=filters,
            snapshot_to=snapshot_to,
            page=page,
            loader=load,
        )
        return _validate_event_list(
            frozen, page.limit, self.snapshots, context.request_id
        )

    async def list_task_events(
        self,
        context: InternalAuthContext,
        *,
        from_at: datetime,
        to_at: datetime,
        snapshot_to: datetime,
        page: PageRequest,
        task_id: str | None = None,
        run_id: str | None = None,
        stage_id: str | None = None,
        event_type: str | None = None,
        level: str | None = None,
        error_code: str | None = None,
    ) -> InternalTaskEventList:
        from_at, to_at, snapshot_to = _validate_window(
            from_at, to_at, snapshot_to, context.request_id
        )
        task_id = _optional_filter(task_id, maximum=80, context=context)
        run_id = _optional_filter(run_id, maximum=80, context=context)
        stage_id = _optional_filter(stage_id, maximum=80, context=context)
        event_type = _optional_filter(event_type, maximum=80, context=context)
        error_code = _optional_filter(error_code, maximum=120, context=context)
        if level is not None and level not in {"DEBUG", "INFO", "WARN", "ERROR"}:
            raise invalid_request(context.request_id)
        filters = {
            "from": from_at.isoformat(),
            "to": to_at.isoformat(),
            "snapshotTo": snapshot_to.isoformat(),
            "taskId": task_id,
            "runId": run_id,
            "stageId": stage_id,
            "eventType": event_type,
            "level": level,
            "errorCode": error_code,
        }

        async def load():
            async with create_db_session() as session:
                statement = (
                    select(TaskEventEntity, TaskEntity)
                    .join(TaskEntity, TaskEntity.id == TaskEventEntity.task_id)
                    .where(TaskEventEntity.occurred_at >= from_at)
                    .where(TaskEventEntity.occurred_at <= to_at)
                    .where(TaskEventEntity.ingested_at <= snapshot_to)
                )
                statement = _scope_statement(statement, TaskEntity.tenant_id, context)
                if task_id is not None:
                    statement = statement.where(TaskEventEntity.task_id == task_id)
                if run_id is not None:
                    statement = statement.where(TaskEventEntity.run_id == run_id)
                if stage_id is not None:
                    statement = statement.where(
                        TaskEventEntity.stage_run_id == stage_id
                    )
                if event_type is not None:
                    statement = statement.where(
                        TaskEventEntity.event_type == event_type
                    )
                if level is not None:
                    storage_levels = {
                        "DEBUG": ("debug",),
                        "INFO": ("info",),
                        "WARN": ("warn", "warning"),
                        "ERROR": ("error",),
                    }[level]
                    statement = statement.where(
                        TaskEventEntity.level.in_(storage_levels)
                    )
                if error_code is not None:
                    statement = statement.where(
                        TaskEventEntity.error_code == error_code
                    )
                pairs = list(
                    (
                        await session.exec(
                            statement.order_by(
                                TaskEventEntity.occurred_at.desc(),
                                TaskEventEntity.id.desc(),
                            ).limit(self.snapshots.max_items + 1)
                        )
                    ).all()
                )
                if len(pairs) > self.snapshots.max_items:
                    raise _snapshot_capacity(context.request_id)
                rows = [pair[0] for pair in pairs]
                tasks = {pair[1].id: pair[1] for pair in pairs}
                await _validate_event_relationships(
                    session, rows, tasks, context.request_id
                )
            return _freeze_events(
                rows, tasks, self.snapshots.max_items, context.request_id
            )

        frozen = await self._materialized_page(
            context,
            resource_kind="task-events",
            snapshot_mode="APPEND_ONLY_HIGH_WATERMARK",
            filters=filters,
            snapshot_to=snapshot_to,
            page=page,
            loader=load,
        )
        return _validate_event_list(
            frozen, page.limit, self.snapshots, context.request_id
        )

    async def list_run_stages(
        self,
        context: InternalAuthContext,
        run_id: str,
    ) -> InternalStageList:
        run, task = await self._authorized_run(context, run_id)
        async with create_db_session() as session:
            rows = list(
                (
                    await session.exec(
                        select(TaskStageRunEntity)
                        .where(TaskStageRunEntity.run_id == run.id)
                        .order_by(
                            TaskStageRunEntity.created_at.asc(),
                            TaskStageRunEntity.id.asc(),
                        )
                    )
                ).all()
            )
        data: list[InternalStage] = []
        for index, row in enumerate(rows, start=1):
            if row.task_id != task.id or row.run_id != run.id:
                raise invalid_source(context.request_id)
            try:
                data.append(
                    InternalStage(
                        stage_id=_source_text(row.id, maximum=80),
                        run_id=_source_text(row.run_id, maximum=80),
                        name=_source_text(row.stage_id, maximum=120),
                        status=_source_text(row.status, maximum=32),
                        sequence=index,
                        started_at=_optional_utc_value(row.started_at),
                        finished_at=_optional_utc_value(row.finished_at),
                        duration_ms=_optional_nonnegative_int(row.duration_ms),
                        retry_count=_stage_retry_count(row.attempt),
                        error_code=_optional_error_code(row.error_code),
                    )
                )
            except (TypeError, ValueError, ValidationError):
                raise invalid_source(context.request_id) from None
        return InternalStageList(data=data)

    async def list_run_events(
        self,
        context: InternalAuthContext,
        *,
        run_id: str,
        snapshot_to: datetime,
        page: PageRequest,
    ) -> InternalTaskEventList:
        run, task = await self._authorized_run(context, run_id)
        snapshot_to = _validate_snapshot_time(snapshot_to, context.request_id)
        filters = {"runId": run.id, "snapshotTo": snapshot_to.isoformat()}

        async def load():
            async with create_db_session() as session:
                rows = list(
                    (
                        await session.exec(
                            select(TaskEventEntity)
                            .where(TaskEventEntity.run_id == run.id)
                            .where(TaskEventEntity.task_id == task.id)
                            .where(TaskEventEntity.ingested_at <= snapshot_to)
                            .order_by(
                                TaskEventEntity.sequence.asc(), TaskEventEntity.id.asc()
                            )
                            .limit(self.snapshots.max_items + 1)
                        )
                    ).all()
                )
                await _validate_event_relationships(
                    session, rows, {task.id: task}, context.request_id
                )
            return _freeze_events(
                rows, {task.id: task}, self.snapshots.max_items, context.request_id
            )

        frozen = await self._materialized_page(
            context,
            resource_kind="run-events",
            snapshot_mode="APPEND_ONLY_HIGH_WATERMARK",
            filters=filters,
            snapshot_to=snapshot_to,
            page=page,
            loader=load,
        )
        return _validate_event_list(
            frozen, page.limit, self.snapshots, context.request_id
        )

    async def list_security_events(
        self,
        context: InternalAuthContext,
        *,
        from_at: datetime,
        to_at: datetime,
        snapshot_to: datetime,
        page: PageRequest,
        filters: dict[str, Any],
    ) -> InternalSecurityEventList:
        from_at, to_at, snapshot_to = _validate_window(
            from_at, to_at, snapshot_to, context.request_id
        )
        filter_limits = {
            "scopeType": 16,
            "category": 80,
            "action": 120,
            "riskLevel": 16,
            "actorId": 120,
            "outcome": 32,
            "subjectType": 80,
            "subjectId": 120,
            "reasonCode": 80,
            "sourceIpMasked": 80,
            "auditActionId": 96,
            "accessSessionId": 96,
            "auditLayer": 32,
        }
        filters = {
            key: (
                _optional_filter(value, maximum=filter_limits[key], context=context)
                if key in filter_limits
                else value
            )
            for key, value in filters.items()
        }
        if filters.get("scopeType") == "SYSTEM" and not context.can_view_system:
            raise InternalObservabilityError(
                403, "INTERNAL_SCOPE_FORBIDDEN", context.request_id
            )
        normalized_filters = {
            "from": from_at.isoformat(),
            "to": to_at.isoformat(),
            "snapshotTo": snapshot_to.isoformat(),
            **filters,
        }

        async def load():
            statement = (
                select(SecurityAuditEventEntity)
                .where(SecurityAuditEventEntity.occurred_at >= from_at)
                .where(SecurityAuditEventEntity.occurred_at <= to_at)
                .where(SecurityAuditEventEntity.ingested_at <= snapshot_to)
            )
            statement = _security_scope_statement(statement, context)
            column_map = {
                "scopeType": SecurityAuditEventEntity.scope_type,
                "category": SecurityAuditEventEntity.category,
                "action": SecurityAuditEventEntity.action,
                "riskLevel": SecurityAuditEventEntity.risk_level,
                "actorId": SecurityAuditEventEntity.actor_id,
                "outcome": SecurityAuditEventEntity.outcome,
                "subjectType": SecurityAuditEventEntity.subject_type,
                "subjectId": SecurityAuditEventEntity.subject_id,
                "reasonCode": SecurityAuditEventEntity.reason_code,
                "sourceIpMasked": SecurityAuditEventEntity.source_ip_masked,
                "crossTenant": SecurityAuditEventEntity.cross_tenant,
                "auditActionId": SecurityAuditEventEntity.audit_action_id,
                "accessSessionId": SecurityAuditEventEntity.access_session_id,
                "auditLayer": SecurityAuditEventEntity.audit_layer,
            }
            for key, column in column_map.items():
                value = filters.get(key)
                if value is not None:
                    statement = statement.where(column == value)
            rows = await self.security.list_rows(
                statement.order_by(
                    SecurityAuditEventEntity.occurred_at.desc(),
                    SecurityAuditEventEntity.id.desc(),
                ).limit(self.snapshots.max_items + 1)
            )
            if len(rows) > self.snapshots.max_items:
                raise _snapshot_capacity(context.request_id)
            try:
                items = [
                    self.security.to_dto(row).model_dump(by_alias=True, mode="json")
                    for row in rows
                ]
            except (TypeError, ValueError, ValidationError):
                raise invalid_source(context.request_id) from None
            max_ingested_at, max_event_id = _max_ingestion_pair(rows)
            return (
                items,
                max_ingested_at,
                None,
                max_event_id,
            )

        frozen = await self._materialized_page(
            context,
            resource_kind="security-events",
            snapshot_mode="APPEND_ONLY_HIGH_WATERMARK",
            filters=normalized_filters,
            snapshot_to=snapshot_to,
            page=page,
            loader=load,
        )
        try:
            return InternalSecurityEventList(
                data=[
                    InternalSecurityEvent.model_validate(item) for item in frozen.items
                ],
                page=self._page_info(frozen, page.limit),
                watermark=self.snapshots.watermark(frozen.row),
            )
        except ValidationError:
            raise invalid_source(context.request_id) from None

    async def get_security_event(
        self,
        context: InternalAuthContext,
        event_id: str,
    ) -> InternalSecurityEvent:
        event_id = _required_identifier(
            event_id, maximum=80, request_id=context.request_id
        )
        row = await self.security.get(event_id)
        if row is None:
            raise invisible(context.request_id, exists=False)
        permitted = (
            context.can_view_system
            if row.scope_type == "SYSTEM"
            else context.permits_tenant(row.tenant_id)
        )
        if not permitted:
            raise invisible(context.request_id, exists=True)
        try:
            return self.security.to_dto(row)
        except (TypeError, ValueError, ValidationError):
            raise invalid_source(context.request_id) from None

    async def authorize_run(
        self,
        context: InternalAuthContext,
        run_id: str,
    ) -> tuple[TaskRunEntity, TaskEntity]:
        return await self._authorized_run(context, run_id)

    async def _authorized_task(
        self,
        context: InternalAuthContext,
        task_id: str,
    ) -> TaskEntity:
        task_id = _required_identifier(
            task_id, maximum=80, request_id=context.request_id
        )
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
        if task is None:
            raise invisible(context.request_id, exists=False)
        if not context.permits_tenant(task.tenant_id):
            raise invisible(context.request_id, exists=True)
        return task

    async def _authorized_run(
        self,
        context: InternalAuthContext,
        run_id: str,
    ) -> tuple[TaskRunEntity, TaskEntity]:
        run_id = _required_identifier(run_id, maximum=80, request_id=context.request_id)
        async with create_db_session() as session:
            run = await session.get(TaskRunEntity, run_id)
            if run is None:
                raise invisible(context.request_id, exists=False)
            task = await session.get(TaskEntity, run.task_id)
        if task is None:
            raise invalid_source(context.request_id)
        if not context.permits_tenant(task.tenant_id):
            raise invisible(context.request_id, exists=True)
        return run, task

    async def _materialized_page(
        self,
        context: InternalAuthContext,
        *,
        resource_kind: str,
        snapshot_mode: str,
        filters: dict[str, Any],
        snapshot_to: datetime,
        page: PageRequest,
        loader: Callable,
    ) -> FrozenPage:
        if (
            isinstance(page.limit, bool)
            or not isinstance(page.limit, int)
            or page.limit < 1
            or page.limit > 200
        ):
            raise invalid_request(context.request_id)
        try:
            filter_hash = canonical_hash(filters)
        except (TypeError, ValueError):
            raise invalid_request(context.request_id) from None
        row, position = await self.snapshots.resolve(
            resource_kind=resource_kind,
            scope_hash=context.scope_fingerprint,
            filter_hash=filter_hash,
            high_watermark=page.high_watermark,
            cursor=page.cursor,
            retry_token=page.retry_token,
            request_id=context.request_id,
        )
        if row is None:
            items, max_ingested_at, max_sequence, max_event_id = await loader()
            row = await self.snapshots.create(
                resource_kind=resource_kind,
                snapshot_mode=snapshot_mode,
                scope_hash=context.scope_fingerprint,
                filter_hash=filter_hash,
                snapshot_to=snapshot_to,
                payload={"items": items},
                item_count=len(items),
                request_id=context.request_id,
                max_ingested_at=max_ingested_at,
                max_sequence=max_sequence,
                max_event_id=max_event_id,
            )
        payload = row.payload_json
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise invalid_source(context.request_id)
        all_items = payload["items"]
        if not all(isinstance(item, dict) for item in all_items):
            raise invalid_source(context.request_id)
        if position > len(all_items):
            raise invalid_request(context.request_id, "OBSERVABILITY_CURSOR_INVALID")
        next_position = min(position + page.limit, len(all_items))
        return FrozenPage(
            row=row,
            items=all_items[position:next_position],
            position=position,
            next_position=next_position,
            has_more=next_position < len(all_items),
        )

    def _page_info(self, frozen: FrozenPage, limit: int) -> PageInfo:
        return PageInfo(
            next_cursor=(
                self.snapshots.next_cursor(frozen.row, frozen.next_position)
                if frozen.has_more
                else None
            ),
            has_more=frozen.has_more,
            limit=limit,
        )


def _scope_statement(statement, tenant_column, context: InternalAuthContext):
    if context.tenant_scope == "ALL":
        return statement
    return statement.where(tenant_column.in_(tuple(context.tenant_ids)))


def _security_scope_statement(statement, context: InternalAuthContext):
    if context.tenant_scope == "ALL":
        tenant_condition = SecurityAuditEventEntity.scope_type == "TENANT"
    else:
        tenant_condition = and_(
            SecurityAuditEventEntity.scope_type == "TENANT",
            SecurityAuditEventEntity.tenant_id.in_(tuple(context.tenant_ids)),
        )
    if context.can_view_system:
        return statement.where(
            or_(tenant_condition, SecurityAuditEventEntity.scope_type == "SYSTEM")
        )
    return statement.where(tenant_condition)


async def _validate_event_relationships(
    session,
    rows: Sequence[TaskEventEntity],
    tasks: Mapping[str, TaskEntity],
    request_id: str,
) -> None:
    run_ids = {row.run_id for row in rows if row.run_id is not None}
    stage_ids = {row.stage_run_id for row in rows if row.stage_run_id is not None}
    runs: dict[str, TaskRunEntity] = {}
    stages: dict[str, TaskStageRunEntity] = {}
    if run_ids:
        run_rows = list(
            (
                await session.exec(
                    select(TaskRunEntity).where(TaskRunEntity.id.in_(run_ids))
                )
            ).all()
        )
        runs = {row.id: row for row in run_rows}
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
    for event in rows:
        task = tasks.get(event.task_id)
        if task is None or event.tenant_id != task.tenant_id:
            raise invalid_source(request_id)
        if event.run_id is None:
            if event.stage_run_id is not None:
                raise invalid_source(request_id)
            continue
        run = runs.get(event.run_id)
        if run is None or run.task_id != task.id:
            raise invalid_source(request_id)
        if event.stage_run_id is not None:
            stage = stages.get(event.stage_run_id)
            if stage is None or stage.task_id != task.id or stage.run_id != run.id:
                raise invalid_source(request_id)


def _task_dto(
    task: TaskEntity,
    run: TaskRunEntity | None,
    data_as_of: datetime,
    request_id: str,
) -> InternalTask:
    try:
        metadata = task.metadata_json
        if not isinstance(metadata, dict):
            raise ValueError
        task_id = _source_text(task.id, maximum=80)
        tenant_id = _source_text(task.tenant_id, maximum=64)
        feature_code = _source_text(task.task_type, maximum=120)
        user_id = _source_text(task.user_id, maximum=120)
        status = _source_text(task.status, maximum=32)
        created_at = _utc_value(task.created_at)
        updated_at = _utc_value(task.updated_at)
        if updated_at < created_at:
            raise ValueError
        subject_type = _metadata_optional_text(
            metadata, "subject_type", "subjectType", maximum=80
        )
        subject_id = _metadata_optional_text(
            metadata, "subject_id", "subjectId", maximum=120
        )
        if (subject_type is None) != (subject_id is None):
            raise ValueError
        if (
            isinstance(task.progress_current, bool)
            or isinstance(task.progress_total, bool)
            or not isinstance(task.progress_current, int)
            or not isinstance(task.progress_total, int)
            or task.progress_current < 0
            or task.progress_total < 0
            or task.progress_current > task.progress_total
        ):
            raise ValueError
        progress = (
            None
            if task.progress_total == 0
            else 100.0 * task.progress_current / task.progress_total
        )
        if task.progress_total == 0 and task.progress_current != 0:
            raise ValueError
        trace_ids = _metadata_trace_ids(metadata)
        trace_id = _metadata_optional_trace_id(metadata, "trace_id", "traceId")
        if trace_id is not None and trace_id not in trace_ids:
            trace_ids.append(trace_id)
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError
        attempt_count = _nonnegative_int(task.attempt_count)
        retry_count = attempt_count - 1 if attempt_count > 0 else 0
        current_run = None
        if run is not None:
            if run.task_id != task_id or attempt_count < 1:
                raise ValueError
            current_run = InternalRun(
                run_id=_source_text(run.id, maximum=80),
                status=_source_text(run.status, maximum=32),
                attempt_no=attempt_count,
                started_at=_optional_utc_value(run.started_at),
                finished_at=_optional_utc_value(run.finished_at),
            )
        return InternalTask(
            task_id=task_id,
            projection_version=_projection_version(task.updated_at),
            data_as_of=data_as_of,
            feature_code=feature_code,
            tenant_id=tenant_id,
            initiator=Actor(type="USER", id=user_id),
            subject=(
                Subject(type=subject_type, id=subject_id)
                if subject_type is not None
                else None
            ),
            status=status,
            stage=(
                _source_text(run.current_stage_id, maximum=120)
                if run is not None and run.current_stage_id is not None
                else None
            ),
            progress=progress,
            created_at=created_at,
            started_at=_optional_utc_value(task.started_at),
            finished_at=_optional_utc_value(task.finished_at),
            duration_ms=_duration_ms(task.started_at, task.finished_at),
            retry_count=retry_count,
            privacy_mode=_metadata_optional_enum(
                metadata,
                {"STANDARD", "PRIVATE"},
                "privacy_mode",
                "privacyMode",
            ),
            route_type=_metadata_optional_enum(
                metadata,
                {"LOCAL", "EXTERNAL"},
                "route_type",
                "routeType",
            ),
            current_run=current_run,
            ingested_at=updated_at,
            request_id=_metadata_optional_text(
                metadata, "request_id", "requestId", maximum=120
            ),
            trace_ids=trace_ids,
            error_code=_task_error_code(task),
        )
    except (TypeError, ValueError, ValidationError):
        raise invalid_source(request_id) from None


def _event_dto(
    event: TaskEventEntity,
    task: TaskEntity,
    request_id: str,
) -> InternalTaskEvent:
    try:
        event_id = _source_text(event.id, maximum=80)
        task_id = _source_text(event.task_id, maximum=80)
        if task_id != task.id or event.tenant_id != task.tenant_id:
            raise ValueError
        sequence = _positive_int(event.sequence)
        schema_version = _schema_version(event.schema_version)
        occurred_at = _utc_value(event.occurred_at)
        ingested_at = _utc_value(event.ingested_at)
        if ingested_at < occurred_at:
            raise ValueError
        event_type = _source_text(event.event_type, maximum=80)
        source = sanitize_task_event_source(event.source_json)
        tool_name = None
        if source.get("type") == "tool":
            tool_name = _source_text(
                source.get("name")
                or source.get("tool_name")
                or source.get("toolName")
                or source.get("id"),
                maximum=120,
            )
        payload = event.payload_json
        if not isinstance(payload, dict):
            raise ValueError
        level = {
            "debug": "DEBUG",
            "info": "INFO",
            "warn": "WARN",
            "warning": "WARN",
            "error": "ERROR",
        }.get(event.level)
        if level is None:
            raise ValueError
        return InternalTaskEvent(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            schema_version=schema_version,
            tenant_id=_source_text(task.tenant_id, maximum=64),
            occurred_at=occurred_at,
            ingested_at=ingested_at,
            task_id=task_id,
            run_id=_optional_source_text(event.run_id, maximum=80),
            stage_id=_optional_source_text(event.stage_run_id, maximum=80),
            agent_name=_optional_source_text(event.agent_id, maximum=80),
            tool_name=tool_name,
            request_id=_optional_source_text(event.request_id, maximum=120),
            trace_id=_optional_trace_id(event.trace_id),
            level=level,
            display_code=event_type.upper(),
            outcome=_event_outcome(event, payload),
            error_code=_optional_error_code(event.error_code),
            duration_ms=_optional_nonnegative_int(event.duration_ms),
            metadata=metadata_fields(
                payload, event_type=event_type, schema_version=schema_version
            ),
        )
    except (RegistryValidationError, TypeError, ValueError, ValidationError):
        raise invalid_source(request_id) from None


def task_event_to_dto(
    event: TaskEventEntity,
    task: TaskEntity,
    request_id: str,
) -> InternalTaskEvent:
    return _event_dto(event, task, request_id)


def _freeze_events(
    rows: Sequence[TaskEventEntity],
    tasks: Mapping[str, TaskEntity],
    max_items: int,
    request_id: str,
):
    if len(rows) > max_items:
        raise _snapshot_capacity(request_id)
    items = [
        _event_dto(row, tasks[row.task_id], request_id).model_dump(
            by_alias=True, mode="json"
        )
        for row in rows
    ]
    max_ingested_at, max_event_id = _max_ingestion_pair(rows)
    return (
        items,
        max_ingested_at,
        _max_sequence(rows),
        max_event_id,
    )


def _max_ingestion_pair(
    rows: Sequence[TaskEventEntity | SecurityAuditEventEntity],
) -> tuple[datetime | None, str | None]:
    if not rows:
        return None, None
    if any(row.ingested_at is None for row in rows):
        raise ValueError("source ingestion timestamp is missing")
    row = max(rows, key=lambda item: (item.ingested_at, item.id))
    return row.ingested_at, row.id


def _max_sequence(rows: Sequence[TaskEventEntity]) -> int | None:
    if not rows:
        return None
    return max(_positive_int(row.sequence) for row in rows)


def _event_outcome(
    event: TaskEventEntity,
    payload: Mapping[str, Any],
) -> str | None:
    raw = payload.get("outcome")
    if raw is not None:
        if not isinstance(raw, str):
            raise ValueError
        normalized = raw.upper()
        if normalized not in {
            "SUCCESS",
            "FAILURE",
            "DENIED",
            "CANCELLED",
            "TIMEOUT",
            "PARTIAL",
            "UNKNOWN",
            "ABANDONED",
        }:
            raise ValueError
        return normalized
    event_type = event.event_type
    if event_type.endswith(("_succeeded", "_completed")):
        return "SUCCESS"
    if event_type.endswith(("_failed", "_error")):
        return "FAILURE"
    if event_type.endswith("_cancelled"):
        return "CANCELLED"
    return None


def _schema_version(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        if value < 1:
            raise ValueError
        return value
    if not isinstance(value, str):
        raise ValueError
    matched = SCHEMA_VERSION_PATTERN.fullmatch(value)
    if not matched:
        raise ValueError
    return int(matched.group("major"))


def _task_error_code(task: TaskEntity) -> str | None:
    payload = task.error_payload_json
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError
    value = payload.get("code") or payload.get("error_code") or payload.get("type")
    return _optional_error_code(value)


def _projection_version(updated_at: datetime) -> int:
    normalized = _utc_value(updated_at)
    return max(1, int(normalized.timestamp() * 1_000_000))


def _duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
) -> int | None:
    if started_at is None or finished_at is None:
        return None
    started = _utc_value(started_at)
    finished = _utc_value(finished_at)
    if finished < started:
        raise ValueError
    return int((finished - started).total_seconds() * 1000)


def _metadata_optional_text(
    metadata: Mapping[str, Any],
    *keys: str,
    maximum: int,
) -> str | None:
    present = [key for key in keys if key in metadata]
    if len(present) > 1:
        values = [metadata[key] for key in present]
        if any(value != values[0] for value in values[1:]):
            raise ValueError
    if not present:
        return None
    return _source_text(metadata[present[0]], maximum=maximum)


def _metadata_trace_ids(metadata: Mapping[str, Any]) -> list[str]:
    present = [key for key in ("trace_ids", "traceIds") if key in metadata]
    if len(present) > 1 and metadata[present[0]] != metadata[present[1]]:
        raise ValueError
    if not present:
        return []
    value = metadata[present[0]]
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not TRACE_ID_PATTERN.fullmatch(item):
            raise ValueError
        result.append(item)
    return result


def _metadata_optional_trace_id(
    metadata: Mapping[str, Any],
    *keys: str,
) -> str | None:
    value = _metadata_optional_text(metadata, *keys, maximum=32)
    return _optional_trace_id(value)


def _metadata_optional_enum(
    metadata: Mapping[str, Any],
    allowed: set[str],
    *keys: str,
) -> str | None:
    value = _metadata_optional_text(metadata, *keys, maximum=16)
    if value is None:
        return None
    if value not in allowed:
        raise ValueError
    return value


def _validate_window(
    from_at: datetime,
    to_at: datetime,
    snapshot_to: datetime,
    request_id: str,
) -> tuple[datetime, datetime, datetime]:
    from_at = _db_time(from_at)
    to_at = _db_time(to_at)
    snapshot_to = _db_time(snapshot_to)
    # from/to constrain occurred_at; snapshotTo independently freezes ingestion.
    if from_at > to_at or snapshot_to > utc_now() + timedelta(seconds=5):
        raise invalid_request(request_id, "OBSERVABILITY_TIME_WINDOW_INVALID")
    return from_at, to_at, snapshot_to


def _validate_snapshot_time(value: datetime, request_id: str) -> datetime:
    value = _db_time(value)
    if value > utc_now() + timedelta(seconds=5):
        raise invalid_request(request_id, "OBSERVABILITY_TIME_WINDOW_INVALID")
    return value


def _db_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_value(value: datetime | None) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError
    return (
        value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    )


def _optional_utc_value(value: datetime | None) -> datetime | None:
    return None if value is None else _utc_value(value)


def _source_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or contains_sensitive_material(normalized)
    ):
        raise ValueError
    return normalized


def _optional_source_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _source_text(value, maximum=maximum)


def _optional_error_code(value: object) -> str | None:
    if value is None:
        return None
    source = _source_text(value, maximum=120)
    canonical = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", source)
    canonical = re.sub(r"[^A-Za-z0-9]+", "_", canonical).strip("_").upper()
    if (
        not canonical
        or len(canonical) > 120
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", canonical)
    ):
        raise ValueError
    return canonical


def _optional_trace_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not TRACE_ID_PATTERN.fullmatch(value):
        raise ValueError
    return value


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value)


def _stage_retry_count(attempt: object) -> int:
    return _positive_int(attempt) - 1


def _optional_filter(
    value: str | None,
    *,
    maximum: int,
    context: InternalAuthContext,
) -> str | None:
    if value is None:
        return None
    try:
        normalized = _source_text(value, maximum=maximum)
        if contains_sensitive_material(normalized):
            raise ValueError
        return normalized
    except ValueError:
        raise invalid_request(context.request_id) from None


def _required_identifier(
    value: str,
    *,
    maximum: int,
    request_id: str,
) -> str:
    try:
        normalized = _source_text(value, maximum=maximum)
        if contains_sensitive_material(normalized):
            raise ValueError
        return normalized
    except ValueError:
        raise invalid_request(request_id) from None


def _snapshot_capacity(request_id: str) -> InternalObservabilityError:
    return InternalObservabilityError(
        429,
        "OBSERVABILITY_SNAPSHOT_CAPACITY_EXCEEDED",
        request_id,
        retryable=True,
    )


def _validate_task_list(
    frozen: FrozenPage,
    limit: int,
    snapshots: SnapshotStore,
    request_id: str,
) -> InternalTaskList:
    try:
        return InternalTaskList(
            data=[InternalTask.model_validate(item) for item in frozen.items],
            page=PageInfo(
                next_cursor=(
                    snapshots.next_cursor(frozen.row, frozen.next_position)
                    if frozen.has_more
                    else None
                ),
                has_more=frozen.has_more,
                limit=limit,
            ),
            watermark=snapshots.watermark(frozen.row),
        )
    except ValidationError:
        raise invalid_source(request_id) from None


def _validate_event_list(
    frozen: FrozenPage,
    limit: int,
    snapshots: SnapshotStore,
    request_id: str,
) -> InternalTaskEventList:
    try:
        return InternalTaskEventList(
            data=[InternalTaskEvent.model_validate(item) for item in frozen.items],
            page=PageInfo(
                next_cursor=(
                    snapshots.next_cursor(frozen.row, frozen.next_position)
                    if frozen.has_more
                    else None
                ),
                has_more=frozen.has_more,
                limit=limit,
            ),
            watermark=snapshots.watermark(frozen.row),
        )
    except ValidationError:
        raise invalid_source(request_id) from None
