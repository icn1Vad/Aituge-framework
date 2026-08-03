from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from datetime import datetime, timedelta
from typing import Any, Generic, TypeVar

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .domain import ModelSnapshotCapacityError, ModelSnapshotError, new_id, utc_now
from .entities import ModelQuerySnapshotEntity


T = TypeVar("T")
SessionContextFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_GLOBAL_SNAPSHOT_LOCK_ID = 1_827_004


@dataclass(frozen=True, slots=True)
class SnapshotPage(Generic[T]):
    rows: tuple[T, ...]
    next_cursor: str | None
    has_more: bool
    snapshot: "StoredSnapshot[T]"


@dataclass(frozen=True, slots=True)
class StoredSnapshot(Generic[T]):
    query_snapshot_id: str
    high_watermark_handle: str
    scope_key: str
    query_hash: str
    rows: tuple[T, ...]
    snapshot_to: datetime
    created_at: datetime
    expires_at: datetime
    snapshot_mode: str
    max_ingested_at: datetime | None
    max_event_id: str | None
    max_sequence: int | None
    data_through: datetime | None
    cursor_signing_key: str


class ModelSnapshotStore:
    """Shared PostgreSQL materialized snapshot store with bounded capacity."""

    def __init__(
        self,
        session_factory: SessionContextFactory,
        *,
        ttl: timedelta = timedelta(minutes=15),
        reuse_window: timedelta = timedelta(seconds=30),
        max_rows: int = 10_000,
        max_active_per_scope: int = 5,
        max_active_global: int = 1_000,
        max_total_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if ttl.total_seconds() <= 0 or reuse_window.total_seconds() < 0:
            raise ValueError("snapshot durations must be non-negative")
        if min(
            max_rows,
            max_active_per_scope,
            max_active_global,
            max_total_bytes,
        ) < 1:
            raise ValueError("snapshot capacity limits must be positive")
        self._session_factory = session_factory
        self._ttl = ttl
        self._reuse_window = reuse_window
        self.max_rows = max_rows
        self._max_active_per_scope = max_active_per_scope
        self._max_active_global = max_active_global
        self._max_total_bytes = max_total_bytes

    async def create_or_reuse(
        self,
        *,
        scope_key: str,
        query_hash: str,
        rows: list[T],
        snapshot_to: datetime,
        snapshot_mode: str,
        max_ingested_at: datetime | None,
        max_event_id: str | None,
        max_sequence: int | None,
        data_through: datetime | None,
    ) -> StoredSnapshot[T]:
        if len(rows) > self.max_rows:
            raise ModelSnapshotCapacityError(
                f"snapshot contains more than {self.max_rows} rows"
            )
        serialized_rows = [_serialize_row(row) for row in rows]
        storage_bytes = len(
            json.dumps(
                serialized_rows,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if storage_bytes > self._max_total_bytes:
            raise ModelSnapshotCapacityError("snapshot exceeds shared storage budget")
        now = utc_now()
        scope_hash = _scope_hash(scope_key)
        async with self._session_factory() as session:
            await self._lock_capacity(session, scope_hash)
            await session.execute(
                delete(ModelQuerySnapshotEntity).where(
                    ModelQuerySnapshotEntity.expires_at <= now
                )
            )
            reusable = (
                await session.execute(
                    select(ModelQuerySnapshotEntity)
                    .where(
                        ModelQuerySnapshotEntity.scope_hash == scope_hash,
                        ModelQuerySnapshotEntity.query_hash == query_hash,
                        ModelQuerySnapshotEntity.created_at
                        >= now - self._reuse_window,
                        ModelQuerySnapshotEntity.expires_at > now,
                    )
                    .order_by(ModelQuerySnapshotEntity.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if reusable is not None:
                return _snapshot_from_entity(reusable, scope_key)

            scoped = list(
                (
                    await session.execute(
                        select(ModelQuerySnapshotEntity)
                        .where(
                            ModelQuerySnapshotEntity.scope_hash == scope_hash,
                            ModelQuerySnapshotEntity.expires_at > now,
                        )
                        .order_by(ModelQuerySnapshotEntity.created_at.asc())
                    )
                ).scalars()
            )
            while len(scoped) >= self._max_active_per_scope:
                victim = scoped.pop(0)
                await session.delete(victim)
                await session.flush()

            active_count = (
                await session.execute(
                    select(func.count())
                    .select_from(ModelQuerySnapshotEntity)
                    .where(ModelQuerySnapshotEntity.expires_at > now)
                )
            ).scalar_one()
            if int(active_count) >= self._max_active_global:
                raise ModelSnapshotCapacityError(
                    "global active snapshot capacity is exhausted"
                )

            used_bytes = (
                await session.execute(
                    select(func.coalesce(func.sum(ModelQuerySnapshotEntity.storage_bytes), 0))
                    .where(ModelQuerySnapshotEntity.expires_at > now)
                )
            ).scalar_one()
            if int(used_bytes) + storage_bytes > self._max_total_bytes:
                raise ModelSnapshotCapacityError(
                    "shared snapshot storage capacity is exhausted"
                )

            entity = ModelQuerySnapshotEntity(
                high_watermark_handle=new_id("hwm"),
                query_snapshot_id=new_id("snap"),
                scope_hash=scope_hash,
                query_hash=query_hash,
                cursor_signing_key=secrets.token_hex(32),
                rows_json=serialized_rows,
                row_count=len(serialized_rows),
                storage_bytes=storage_bytes,
                snapshot_to=snapshot_to,
                created_at=now,
                expires_at=now + self._ttl,
                snapshot_mode=snapshot_mode,
                max_ingested_at=max_ingested_at,
                max_event_id=max_event_id,
                max_sequence=max_sequence,
                data_through=data_through,
            )
            session.add(entity)
            await session.flush()
            return _snapshot_from_entity(entity, scope_key)

    async def page(
        self,
        *,
        high_watermark_handle: str,
        cursor: str | None,
        scope_key: str,
        query_hash: str,
        limit: int,
    ) -> SnapshotPage[T]:
        now = utc_now()
        async with self._session_factory() as session:
            snapshot_entity = await session.get(
                ModelQuerySnapshotEntity,
                high_watermark_handle,
            )
            if (
                snapshot_entity is None
                or _aware(snapshot_entity.expires_at) <= now
            ):
                raise ModelSnapshotError("highWatermark is unknown or expired")
            if (
                snapshot_entity.scope_hash != _scope_hash(scope_key)
                or snapshot_entity.query_hash != query_hash
            ):
                raise ModelSnapshotError("highWatermark does not match query scope")
            snapshot = _snapshot_from_entity(snapshot_entity, scope_key)

        offset = 0
        if cursor:
            offset = _decode_cursor(cursor, snapshot)
        if offset < 0 or offset > len(snapshot.rows):
            raise ModelSnapshotError("cursor offset is invalid")
        end = min(offset + limit, len(snapshot.rows))
        rows = snapshot.rows[offset:end]
        has_more = end < len(snapshot.rows)
        next_cursor = _encode_cursor(snapshot, end) if has_more else None
        return SnapshotPage(
            rows=rows,
            next_cursor=next_cursor,
            has_more=has_more,
            snapshot=snapshot,
        )

    async def first_page(
        self,
        snapshot: StoredSnapshot[T],
        *,
        limit: int,
    ) -> SnapshotPage[T]:
        return await self.page(
            high_watermark_handle=snapshot.high_watermark_handle,
            cursor=None,
            scope_key=snapshot.scope_key,
            query_hash=snapshot.query_hash,
            limit=limit,
        )

    async def cleanup_expired(self, *, limit: int = 1000) -> int:
        """Delete a bounded batch of expired snapshots for maintenance jobs."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("snapshot cleanup limit must be positive")
        now = utc_now()
        async with self._session_factory() as session:
            expired = list(
                (
                    await session.execute(
                        select(ModelQuerySnapshotEntity.high_watermark_handle)
                        .where(ModelQuerySnapshotEntity.expires_at <= now)
                        .order_by(
                            ModelQuerySnapshotEntity.expires_at,
                            ModelQuerySnapshotEntity.high_watermark_handle,
                        )
                        .limit(limit)
                    )
                ).scalars()
            )
            if not expired:
                return 0
            result = await session.execute(
                delete(ModelQuerySnapshotEntity).where(
                    ModelQuerySnapshotEntity.high_watermark_handle.in_(
                        tuple(expired)
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    @staticmethod
    async def _lock_capacity(session: AsyncSession, scope_hash: str) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _GLOBAL_SNAPSHOT_LOCK_ID},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": f"model-snapshot:{scope_hash}"},
            )
        elif dialect == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported model snapshot dialect: {dialect}")


def stable_query_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _serialize_row(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        raw = value.model_dump(mode="python", by_alias=False)
    elif isinstance(value, dict):
        raw = value
    else:
        raise TypeError("snapshot rows must be contract models or dictionaries")
    return json.loads(
        json.dumps(
            raw,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
            allow_nan=False,
        )
    )


def _snapshot_from_entity(
    entity: ModelQuerySnapshotEntity,
    scope_key: str,
) -> StoredSnapshot[Any]:
    return StoredSnapshot(
        query_snapshot_id=entity.query_snapshot_id,
        high_watermark_handle=entity.high_watermark_handle,
        scope_key=scope_key,
        query_hash=entity.query_hash,
        rows=tuple(dict(row) for row in entity.rows_json),
        snapshot_to=_aware(entity.snapshot_to),
        created_at=_aware(entity.created_at),
        expires_at=_aware(entity.expires_at),
        snapshot_mode=entity.snapshot_mode,
        max_ingested_at=_optional_aware(entity.max_ingested_at),
        max_event_id=entity.max_event_id,
        max_sequence=entity.max_sequence,
        data_through=_optional_aware(entity.data_through),
        cursor_signing_key=entity.cursor_signing_key,
    )


def _encode_cursor(snapshot: StoredSnapshot[Any], offset: int) -> str:
    offset_bytes = offset.to_bytes(8, "big", signed=False)
    message = snapshot.high_watermark_handle.encode("utf-8") + b":" + offset_bytes
    signature = hmac.new(
        bytes.fromhex(snapshot.cursor_signing_key),
        message,
        hashlib.sha256,
    ).digest()[:16]
    payload = base64.urlsafe_b64encode(offset_bytes + signature).decode("ascii")
    return "cur_" + payload.rstrip("=")


def _decode_cursor(cursor: str, snapshot: StoredSnapshot[Any]) -> int:
    if not cursor.startswith("cur_"):
        raise ModelSnapshotError("cursor is malformed")
    encoded = cursor.removeprefix("cur_")
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise ModelSnapshotError("cursor is malformed") from exc
    if len(payload) != 24:
        raise ModelSnapshotError("cursor is malformed")
    offset_bytes, supplied = payload[:8], payload[8:]
    message = snapshot.high_watermark_handle.encode("utf-8") + b":" + offset_bytes
    expected = hmac.new(
        bytes.fromhex(snapshot.cursor_signing_key),
        message,
        hashlib.sha256,
    ).digest()[:16]
    if not hmac.compare_digest(supplied, expected):
        raise ModelSnapshotError("cursor signature is invalid")
    return int.from_bytes(offset_bytes, "big", signed=False)


def _scope_hash(scope_key: str) -> str:
    return hashlib.sha256(scope_key.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("snapshot decimal values must be finite")
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(
        f"unsupported snapshot value type: {value.__class__.__name__}"
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utc_now().tzinfo)


def _optional_aware(value: datetime | None) -> datetime | None:
    return _aware(value) if value is not None else None
