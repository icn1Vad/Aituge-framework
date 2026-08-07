from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from db.db_context import create_db_session
from sqlalchemy import delete, text
from sqlmodel import select

from .capabilities import (
    CapabilitySigner,
    new_high_watermark_handle,
    reject_cursor_retry_conflict,
    require_high_watermark_handle,
)
from .errors import InternalObservabilityError, invalid_request
from .models import QuerySnapshotEntity, new_query_snapshot_id, utc_now
from .schemas import SourceWatermark

# One transaction-wide lock serializes capacity decisions across processes.
_POSTGRES_SNAPSHOT_LOCK_KEY = 7_230_030_001


@dataclass(frozen=True, slots=True)
class SnapshotCapacityStats:
    active_count: int
    active_bytes: int
    expired_count: int
    max_active_per_scope: int
    max_global: int
    max_snapshot_bytes: int
    max_total_bytes: int


class SnapshotStore:
    """Bounded server-side snapshots addressed only through short handles."""

    def __init__(
        self,
        signer: CapabilitySigner,
        *,
        ttl_seconds: int = 900,
        reuse_seconds: int = 30,
        max_active_per_scope: int = 5,
        max_global: int = 1000,
        max_items: int = 10_000,
        max_snapshot_bytes: int = 2 * 1024 * 1024,
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.signer = signer
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.reuse_seconds = max(0, int(reuse_seconds))
        self.max_active_per_scope = max(1, int(max_active_per_scope))
        self.max_global = max(self.max_active_per_scope, int(max_global))
        self.max_items = max(1, int(max_items))
        self.max_snapshot_bytes = max(1024, int(max_snapshot_bytes))
        self.max_total_bytes = max(self.max_snapshot_bytes, int(max_total_bytes))
        self._local_creation_lock = asyncio.Lock()

    async def create(
        self,
        *,
        resource_kind: str,
        snapshot_mode: str,
        scope_hash: str,
        filter_hash: str,
        snapshot_to: datetime,
        payload: dict,
        item_count: int,
        request_id: str,
        max_ingested_at: datetime | None = None,
        max_sequence: int | None = None,
        max_event_id: str | None = None,
    ) -> QuerySnapshotEntity:
        payload_bytes = _validate_snapshot_input(
            resource_kind=resource_kind,
            snapshot_mode=snapshot_mode,
            scope_hash=scope_hash,
            filter_hash=filter_hash,
            payload=payload,
            item_count=item_count,
            max_items=self.max_items,
            max_snapshot_bytes=self.max_snapshot_bytes,
            max_total_bytes=self.max_total_bytes,
            request_id=request_id,
        )
        # SQLite has no advisory lock. This process lock keeps disposable tests
        # deterministic; PostgreSQL additionally takes a DB transaction lock.
        async with self._local_creation_lock:
            return await self._create_locked(
                resource_kind=resource_kind,
                snapshot_mode=snapshot_mode,
                scope_hash=scope_hash,
                filter_hash=filter_hash,
                snapshot_to=_db_time(snapshot_to),
                payload=payload,
                payload_bytes=payload_bytes,
                item_count=item_count,
                request_id=request_id,
                max_ingested_at=(
                    _db_time(max_ingested_at) if max_ingested_at is not None else None
                ),
                max_sequence=max_sequence,
                max_event_id=max_event_id,
            )

    async def _create_locked(
        self,
        *,
        resource_kind: str,
        snapshot_mode: str,
        scope_hash: str,
        filter_hash: str,
        snapshot_to: datetime,
        payload: dict,
        payload_bytes: int,
        item_count: int,
        request_id: str,
        max_ingested_at: datetime | None,
        max_sequence: int | None,
        max_event_id: str | None,
    ) -> QuerySnapshotEntity:
        now = utc_now()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        async with create_db_session() as session:
            backend = session.get_bind().dialect.name
            if backend == "postgresql":
                await session.exec(
                    text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(
                        lock_key=_POSTGRES_SNAPSHOT_LOCK_KEY
                    )
                )
            await session.exec(
                delete(QuerySnapshotEntity).where(QuerySnapshotEntity.expires_at <= now)
            )

            reusable_statement = (
                select(QuerySnapshotEntity)
                .where(QuerySnapshotEntity.resource_kind == resource_kind)
                .where(QuerySnapshotEntity.scope_hash == scope_hash)
                .where(QuerySnapshotEntity.filter_hash == filter_hash)
                .where(QuerySnapshotEntity.snapshot_to == snapshot_to)
                .where(
                    QuerySnapshotEntity.created_at
                    >= now - timedelta(seconds=self.reuse_seconds)
                )
                .order_by(
                    QuerySnapshotEntity.created_at.desc(),
                    QuerySnapshotEntity.handle.desc(),
                )
                .limit(1)
            )
            if backend == "postgresql":
                reusable_statement = reusable_statement.with_for_update()
            reusable = (await session.exec(reusable_statement)).first()
            if reusable is not None:
                await session.commit()
                return reusable

            scope_statement = (
                select(QuerySnapshotEntity)
                .where(QuerySnapshotEntity.scope_hash == scope_hash)
                .order_by(
                    QuerySnapshotEntity.created_at.asc(),
                    QuerySnapshotEntity.handle.asc(),
                )
            )
            global_statement = select(QuerySnapshotEntity).order_by(
                QuerySnapshotEntity.created_at.asc(),
                QuerySnapshotEntity.handle.asc(),
            )
            if backend == "postgresql":
                scope_statement = scope_statement.with_for_update()
                global_statement = global_statement.with_for_update()
            scope_rows = list((await session.exec(scope_statement)).all())
            global_rows = list((await session.exec(global_statement)).all())

            victims: list[QuerySnapshotEntity] = []
            while len(scope_rows) >= self.max_active_per_scope:
                victims.append(scope_rows.pop(0))
            victim_handles = {row.handle for row in victims}
            active_global_rows = [
                row for row in global_rows if row.handle not in victim_handles
            ]
            active_bytes = sum(
                _stored_payload_bytes(row, request_id) for row in active_global_rows
            )
            if (
                len(active_global_rows) >= self.max_global
                or active_bytes + payload_bytes > self.max_total_bytes
            ):
                raise _snapshot_capacity_exceeded(request_id)
            for victim in victims:
                await session.delete(victim)

            row = QuerySnapshotEntity(
                handle=new_high_watermark_handle(),
                query_snapshot_id=new_query_snapshot_id(),
                resource_kind=resource_kind,
                snapshot_mode=snapshot_mode,
                scope_hash=scope_hash,
                filter_hash=filter_hash,
                snapshot_to=snapshot_to,
                expires_at=expires_at,
                max_ingested_at=max_ingested_at,
                max_sequence=max_sequence,
                max_event_id=max_event_id,
                item_count=item_count,
                payload_bytes=payload_bytes,
                payload_json=payload,
                created_at=now,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        # Callable scheduler boundary; creation also performs opportunistic cleanup.
        cutoff = _db_time(now or utc_now())
        async with self._local_creation_lock:
            async with create_db_session() as session:
                if session.get_bind().dialect.name == "postgresql":
                    await session.exec(
                        text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(
                            lock_key=_POSTGRES_SNAPSHOT_LOCK_KEY
                        )
                    )
                handles = list(
                    (
                        await session.exec(
                            select(QuerySnapshotEntity.handle).where(
                                QuerySnapshotEntity.expires_at <= cutoff
                            )
                        )
                    ).all()
                )
                await session.exec(
                    delete(QuerySnapshotEntity).where(
                        QuerySnapshotEntity.expires_at <= cutoff
                    )
                )
                await session.commit()
                return len(handles)

    async def capacity_stats(
        self, *, now: datetime | None = None, request_id: str = "req_snapshot_stats"
    ) -> SnapshotCapacityStats:
        # DB-backed facts consumed by the later metrics adapter.
        cutoff = _db_time(now or utc_now())
        async with create_db_session() as session:
            rows = list((await session.exec(select(QuerySnapshotEntity))).all())
        active = [row for row in rows if row.expires_at > cutoff]
        return SnapshotCapacityStats(
            active_count=len(active),
            active_bytes=sum(_stored_payload_bytes(row, request_id) for row in active),
            expired_count=len(rows) - len(active),
            max_active_per_scope=self.max_active_per_scope,
            max_global=self.max_global,
            max_snapshot_bytes=self.max_snapshot_bytes,
            max_total_bytes=self.max_total_bytes,
        )

    async def get(
        self,
        handle: str,
        *,
        resource_kind: str,
        scope_hash: str,
        filter_hash: str,
        request_id: str,
    ) -> QuerySnapshotEntity:
        require_high_watermark_handle(handle, request_id)
        async with create_db_session() as session:
            row = await session.get(QuerySnapshotEntity, handle)
            if row is None:
                raise invalid_request(
                    request_id, "OBSERVABILITY_HIGH_WATERMARK_INVALID"
                )
            if row.expires_at <= utc_now():
                await session.delete(row)
                await session.commit()
                raise invalid_request(request_id, "OBSERVABILITY_SNAPSHOT_EXPIRED")
            if (
                row.resource_kind != resource_kind
                or row.scope_hash != scope_hash
                or row.filter_hash != filter_hash
            ):
                raise invalid_request(
                    request_id, "OBSERVABILITY_HIGH_WATERMARK_INVALID"
                )
            return row

    async def resolve(
        self,
        *,
        resource_kind: str,
        scope_hash: str,
        filter_hash: str,
        high_watermark: str | None,
        cursor: str | None,
        retry_token: str | None,
        request_id: str,
    ) -> tuple[QuerySnapshotEntity | None, int]:
        reject_cursor_retry_conflict(cursor, retry_token, request_id)
        handle = require_high_watermark_handle(high_watermark, request_id)
        if cursor is not None or retry_token is not None:
            if handle is None:
                raise invalid_request(
                    request_id, "OBSERVABILITY_HIGH_WATERMARK_REQUIRED"
                )
            row = await self.get(
                handle,
                resource_kind=resource_kind,
                scope_hash=scope_hash,
                filter_hash=filter_hash,
                request_id=request_id,
            )
            raw = cursor if cursor is not None else retry_token or ""
            kind = "cursor" if cursor is not None else "retry"
            claims = self.signer.verify(
                raw,
                expected_kind=kind,
                expected_handle=handle,
                expected_scope_hash=scope_hash,
                expected_filter_hash=filter_hash,
                request_id=request_id,
            )
            return row, claims.position
        if handle is None:
            return None, 0
        row = await self.get(
            handle,
            resource_kind=resource_kind,
            scope_hash=scope_hash,
            filter_hash=filter_hash,
            request_id=request_id,
        )
        return row, 0

    def next_cursor(self, row: QuerySnapshotEntity, position: int) -> str:
        return self.signer.issue(
            kind="cursor",
            handle=row.handle,
            position=position,
            scope_hash=row.scope_hash,
            filter_hash=row.filter_hash,
            expires_at=_utc_epoch(row.expires_at),
        )

    def retry_token(self, row: QuerySnapshotEntity, position: int) -> str:
        return self.signer.issue(
            kind="retry",
            handle=row.handle,
            position=position,
            scope_hash=row.scope_hash,
            filter_hash=row.filter_hash,
            expires_at=_utc_epoch(row.expires_at),
        )

    @staticmethod
    def watermark(row: QuerySnapshotEntity) -> SourceWatermark:
        return SourceWatermark(
            query_snapshot_id=row.query_snapshot_id,
            snapshot_to=_aware_utc(row.snapshot_to),
            expires_at=_aware_utc(row.expires_at),
            snapshot_mode=row.snapshot_mode,
            high_watermark_handle=row.handle,
            max_ingested_at=(
                _aware_utc(row.max_ingested_at) if row.max_ingested_at else None
            ),
            max_sequence=row.max_sequence,
            max_event_id=row.max_event_id,
            data_through=(
                _aware_utc(row.max_ingested_at) if row.max_ingested_at else None
            ),
        )


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _validate_snapshot_input(
    *,
    resource_kind: str,
    snapshot_mode: str,
    scope_hash: str,
    filter_hash: str,
    payload: dict,
    item_count: int,
    max_items: int,
    max_snapshot_bytes: int,
    max_total_bytes: int,
    request_id: str,
) -> int:
    if (
        snapshot_mode not in {"APPEND_ONLY_HIGH_WATERMARK", "MATERIALIZED_RESULT_SET"}
        or not resource_kind
        or len(resource_kind) > 48
        or len(scope_hash) != 64
        or len(filter_hash) != 64
        or isinstance(item_count, bool)
        or not isinstance(item_count, int)
        or item_count < 0
        or not isinstance(payload, dict)
        or not isinstance(payload.get("items"), list)
        or len(payload["items"]) != item_count
    ):
        raise invalid_request(request_id, "OBSERVABILITY_SNAPSHOT_INVALID")
    try:
        payload_bytes = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        raise invalid_request(request_id, "OBSERVABILITY_SNAPSHOT_INVALID") from None
    if (
        item_count > max_items
        or payload_bytes > max_snapshot_bytes
        or payload_bytes > max_total_bytes
    ):
        raise _snapshot_capacity_exceeded(request_id)
    return payload_bytes


def _snapshot_capacity_exceeded(request_id: str) -> InternalObservabilityError:
    return InternalObservabilityError(
        429,
        "OBSERVABILITY_SNAPSHOT_CAPACITY_EXCEEDED",
        request_id,
        retryable=True,
    )


def _stored_payload_bytes(row: QuerySnapshotEntity, request_id: str) -> int:
    value = row.payload_bytes
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InternalObservabilityError(
            500, "OBSERVABILITY_SNAPSHOT_STATE_INVALID", request_id
        )
    return value


def _utc_epoch(value: datetime) -> int:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(normalized.timestamp())


def _db_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("snapshot timestamp must be datetime")
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
