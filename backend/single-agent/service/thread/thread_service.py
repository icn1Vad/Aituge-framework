from datetime import datetime, timezone
from typing import List, Optional

from common.system_constants import DEFAULT_TENANT_ID
from db.models.message import MessageEntity
from db.models.thread import ThreadCreate, ThreadEntity
from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession


class ThreadService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_thread(
        self,
        thread_data: ThreadCreate | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ThreadEntity:
        thread = ThreadEntity.model_validate(thread_data or ThreadCreate(), update={"tenant_id": tenant_id})
        self.session.add(thread)
        await self.session.flush()
        await self.session.refresh(thread)
        return thread

    async def get_thread(self, thread_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[ThreadEntity]:
        result = await self.session.exec(
            select(ThreadEntity).where(
                ThreadEntity.id == thread_id,
                ThreadEntity.tenant_id == tenant_id,
            )
        )
        return result.first()

    async def list_threads(
        self,
        user_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ThreadEntity]:
        statement = select(ThreadEntity).where(ThreadEntity.tenant_id == tenant_id)
        if user_id:
            statement = statement.where(ThreadEntity.user_id == user_id)
        statement = statement.order_by(ThreadEntity.updated_at.desc()).offset(offset).limit(limit)
        result = await self.session.exec(statement)
        return list(result.all())

    async def update_thread_title(
        self,
        thread_id: str,
        title: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ThreadEntity:
        thread = await self.get_thread(thread_id, tenant_id)
        if not thread:
            raise ValueError(f"Thread '{thread_id}' does not exist.")
        thread.title = title
        thread.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.session.add(thread)
        await self.session.flush()
        await self.session.refresh(thread)
        return thread

    async def delete_thread(self, thread_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        thread = await self.get_thread(thread_id, tenant_id)
        if not thread:
            raise ValueError(f"Thread '{thread_id}' does not exist.")
        await self.session.exec(delete(MessageEntity).where(MessageEntity.thread_id == thread_id))
        await self.session.delete(thread)
        await self.session.flush()
