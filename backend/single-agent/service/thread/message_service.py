from typing import List, Optional

from common.system_constants import DEFAULT_TENANT_ID
from db.models.message import MessageCreate, MessageEntity
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession


class MessageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_message(
        self,
        message_data: MessageCreate,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> MessageEntity:
        if message_data.local_id:
            existing = await self.get_message_by_local_id(
                thread_id=message_data.thread_id,
                local_id=message_data.local_id,
                tenant_id=tenant_id,
            )
            if existing:
                return existing

        message = MessageEntity.model_validate(message_data, update={"tenant_id": tenant_id})
        self.session.add(message)
        await self.session.flush()
        await self.session.refresh(message)
        return message

    async def get_message(self, message_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[MessageEntity]:
        result = await self.session.exec(
            select(MessageEntity).where(
                MessageEntity.id == message_id,
                MessageEntity.tenant_id == tenant_id,
            )
        )
        return result.first()

    async def get_message_by_local_id(
        self,
        thread_id: str,
        local_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Optional[MessageEntity]:
        result = await self.session.exec(
            select(MessageEntity).where(
                MessageEntity.thread_id == thread_id,
                MessageEntity.local_id == local_id,
                MessageEntity.tenant_id == tenant_id,
            )
        )
        return result.first()

    async def list_messages(
        self,
        thread_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> List[MessageEntity]:
        result = await self.session.exec(
            select(MessageEntity)
            .where(
                MessageEntity.thread_id == thread_id,
                MessageEntity.tenant_id == tenant_id,
            )
            .order_by(MessageEntity.created_at.asc())
        )
        return list(result.all())
