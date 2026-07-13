from __future__ import annotations

import uuid

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from sqlmodel import select

from .models import MainAgentSessionEntity, ManagedSingleAgentEntity, ScriptWorkspaceEntity, utc_now


class MainAgentSessionStore:
    async def get_or_create(
        self,
        session_id: str,
        *,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> MainAgentSessionEntity:
        async with create_db_session() as session:
            row = await session.get(MainAgentSessionEntity, session_id)
            if row is not None:
                if row.user_id != user_id or row.tenant_id != tenant_id:
                    raise ValueError(f"MainAgent session '{session_id}' is not available to this user.")
                return row
            row = MainAgentSessionEntity(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def update(
        self,
        session_id: str,
        *,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        thread_id: str | None = None,
        phase: str | None = None,
    ) -> MainAgentSessionEntity:
        async with create_db_session() as session:
            row = await session.get(MainAgentSessionEntity, session_id)
            if row is None or row.user_id != user_id or row.tenant_id != tenant_id:
                raise ValueError(f"MainAgent session '{session_id}' not found.")
            if thread_id is not None:
                row.thread_id = thread_id
            if phase is not None:
                row.phase = phase
            row.updated_at = utc_now()
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row


class ManagedSingleAgentStore:
    async def create(
        self,
        *,
        primary_session_id: str,
        primary_agent_id: str,
        agent_id: str,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ManagedSingleAgentEntity:
        instance_id = uuid.uuid4().hex
        row = ManagedSingleAgentEntity(
            instance_id=instance_id,
            primary_session_id=primary_session_id,
            primary_agent_id=primary_agent_id,
            agent_id=agent_id,
            child_session_id=f"main-agent:{primary_session_id}:{instance_id}",
            user_id=user_id,
            tenant_id=tenant_id,
        )
        async with create_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get(
        self,
        instance_id: str,
        *,
        primary_session_id: str,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ManagedSingleAgentEntity | None:
        async with create_db_session() as session:
            row = await session.get(ManagedSingleAgentEntity, instance_id)
            if row is None:
                return None
            if (
                row.primary_session_id != primary_session_id
                or row.user_id != user_id
                or row.tenant_id != tenant_id
            ):
                return None
            return row

    async def list(
        self,
        *,
        primary_session_id: str,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[ManagedSingleAgentEntity]:
        async with create_db_session() as session:
            result = await session.exec(
                select(ManagedSingleAgentEntity)
                .where(ManagedSingleAgentEntity.primary_session_id == primary_session_id)
                .where(ManagedSingleAgentEntity.user_id == user_id)
                .where(ManagedSingleAgentEntity.tenant_id == tenant_id)
                .order_by(ManagedSingleAgentEntity.created_at)
            )
            return list(result.all())

    async def update_child_thread(self, instance_id: str, thread_id: str) -> None:
        async with create_db_session() as session:
            row = await session.get(ManagedSingleAgentEntity, instance_id)
            if row is None:
                return
            row.child_thread_id = thread_id
            row.updated_at = utc_now()
            session.add(row)
            await session.commit()

    async def bind_primary_thread(
        self,
        primary_session_id: str,
        thread_id: str,
        *,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        async with create_db_session() as session:
            result = await session.exec(
                select(ManagedSingleAgentEntity)
                .where(ManagedSingleAgentEntity.primary_session_id == primary_session_id)
                .where(ManagedSingleAgentEntity.user_id == user_id)
                .where(ManagedSingleAgentEntity.tenant_id == tenant_id)
            )
            for row in result.all():
                row.primary_thread_id = thread_id
                row.updated_at = utc_now()
                session.add(row)
            await session.commit()


class ScriptWorkspaceStore:
    async def create(
        self,
        *,
        script_text: str,
        storyboard_text: str = "",
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ScriptWorkspaceEntity:
        row = ScriptWorkspaceEntity(
            script_text=script_text,
            storyboard_text=storyboard_text,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        async with create_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get(
        self,
        workspace_id: str,
        *,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ScriptWorkspaceEntity | None:
        async with create_db_session() as session:
            row = await session.get(ScriptWorkspaceEntity, workspace_id)
            if row is None or row.user_id != user_id or row.tenant_id != tenant_id:
                return None
            return row

    async def update(
        self,
        workspace_id: str,
        *,
        script_text: str | None = None,
        storyboard_text: str | None = None,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ScriptWorkspaceEntity:
        async with create_db_session() as session:
            row = await session.get(ScriptWorkspaceEntity, workspace_id)
            if row is None or row.user_id != user_id or row.tenant_id != tenant_id:
                raise ValueError(f"Script workspace '{workspace_id}' not found.")
            if script_text is not None:
                row.script_text = script_text
            if storyboard_text is not None:
                row.storyboard_text = storyboard_text
            row.updated_at = utc_now()
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row
