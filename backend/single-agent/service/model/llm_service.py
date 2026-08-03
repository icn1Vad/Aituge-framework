"""LLM Service layer for database operations."""
from typing import Optional, List
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.exc import IntegrityError
from aituge_model.config import load_model_registry
from db.models.llm import LlmModelCreate, LlmModelEntity
from common.encrypt_utils import encrypt_key
from common.chat.response_model import PagedResult
from common.system_constants import DEFAULT_TENANT_ID
from loguru import logger


class LlmService:
    """Service layer for LLM entity CRUD operations using dependency injection."""

    def __init__(self, session: AsyncSession):
        """
        Initialize LlmService with a database session.

        Args:
            session: Database session (injected dependency)
        """
        self.session = session

    async def get_llm(self, llm_id: str, tenant_id: str) -> Optional[LlmModelEntity]:
        """
        Get a single LLM entity by ID.

        Args:
            llm_id: LLM entity ID

        Returns:
            LlmModelEntity if found, None otherwise
        """
        result = await self.session.exec(select(LlmModelEntity).where(LlmModelEntity.id == llm_id, LlmModelEntity.tenant_id == tenant_id))
        return result.first()

    async def get_multimodal_llm(self, tenant_id: str) -> Optional[LlmModelEntity]:
        """
        Get the tenant override, falling back to the shared default model.
        """
        model = await self._get_multimodal_llm_for_tenant(tenant_id)
        if model is not None or tenant_id == DEFAULT_TENANT_ID:
            return model
        return await self._get_multimodal_llm_for_tenant(DEFAULT_TENANT_ID)

    async def _get_multimodal_llm_for_tenant(
        self, tenant_id: str
    ) -> Optional[LlmModelEntity]:
        statement = (
            select(LlmModelEntity)
            .where(
                LlmModelEntity.vision_support,
                LlmModelEntity.enabled,
                LlmModelEntity.tenant_id == tenant_id,
            )
            .order_by(
                LlmModelEntity.enabled.desc(),
                LlmModelEntity.provider_name.asc(),
                LlmModelEntity.model_id.asc(),
                LlmModelEntity.id.asc(),
            )
        )
        result = (await self.session.exec(statement)).first()
        return result

    async def get_llm_by_model_id(self, model_id: str, tenant_id: str) -> Optional[LlmModelEntity]:
        """
        Get a single LLM entity by model_id.

        Args:
            model_id: LLM model_id

        Returns:
            LlmModelEntity if found, None otherwise
        """
        model = await self._get_llm_by_model_id_for_tenant(model_id, tenant_id)
        if model is not None or tenant_id == DEFAULT_TENANT_ID:
            return model
        return await self._get_llm_by_model_id_for_tenant(
            model_id, DEFAULT_TENANT_ID
        )

    async def _get_llm_by_model_id_for_tenant(
        self, model_id: str, tenant_id: str
    ) -> Optional[LlmModelEntity]:
        statement = select(LlmModelEntity).where(
            LlmModelEntity.model_id == model_id,
            LlmModelEntity.tenant_id == tenant_id,
        )
        result = await self.session.exec(statement)
        return result.first()

    async def list_llms(
        self,
        tenant_id: str,
        provider_name: Optional[str] = None,
        page: int = 1,
        size: int = 10,
        vision_support: Optional[bool] = None,
    ) -> PagedResult[List[LlmModelEntity]]:
        """
        List LLM entities with pagination and optional filtering.

        Args:
            page: Page number (1-indexed)
            size: Page size
            vision_support: Optional filter for vision support

        Returns:
            PagedResult containing list of LlmModelEntity and pagination metadata
        """
        # Build base query
        base_query = select(LlmModelEntity).where(LlmModelEntity.tenant_id == tenant_id)

        if provider_name is not None:
            base_query = base_query.where(
                LlmModelEntity.provider_name == provider_name
            )

        # Add vision_support filter if provided
        if vision_support is not None:
            base_query = base_query.where(
                LlmModelEntity.vision_support == vision_support
            )

        # Get total count
        count_query = select(func.count()).select_from(base_query)
        total_result = await self.session.exec(count_query)
        total = total_result.one_or_none() or 0

        # Get paginated results
        offset = (page - 1) * size
        paginated_query = base_query.offset(offset).limit(size)
        results = await self.session.exec(paginated_query)
        llms = list(results.all())

        # Calculate pages
        pages = (total + size - 1) // size if total > 0 else 0

        return PagedResult(
            items=llms,
            total=total,
            pages=pages,
            page=page,
            size=size,
        )

    async def get_provider_names(self, tenant_id: str, vision_support: Optional[bool] = None) -> List[str]:
        """
        Get distinct provider names for LLMs.

        Args:
            tenant_id: Tenant ID
            vision_support: Optional filter for vision support

        Returns:
            List of distinct provider names
        """
        base_query = select(LlmModelEntity.provider_name).where(
            LlmModelEntity.tenant_id == tenant_id
        )
        if vision_support is not None:
            base_query = base_query.where(LlmModelEntity.vision_support == vision_support)
        statement = base_query.distinct()
        result = await self.session.exec(statement)
        providers = [p for p in result.all() if p]
        providers.extend(
            registration.provider
            for registration in load_model_registry().llms.values()
        )
        return sorted(set(providers))

    async def create_llm(self, llm_data: LlmModelCreate, tenant_id: str) -> LlmModelEntity:
        """
        Create a new LLM entity.
        Note: Caller is responsible for committing the session.

        Args:
            llm_data: LLM creation data

        Returns:
            Created LlmModelEntity (not yet committed)

        Raises:
            ValueError: If model_id already exists (IntegrityError converted)
        """
        registration = load_model_registry().llms.get(llm_data.model_id)
        if registration is None:
            raise ValueError(
                f"LLM creation failed: model_id '{llm_data.model_id}' is not registered."
            )
        encrypted_api_key = encrypt_key(llm_data.api_key) if llm_data.api_key else None

        llm = LlmModelEntity.model_validate(
            llm_data,
            update={
                "tenant_id": tenant_id,
                "base_url": registration.base_url,
                "model": registration.model,
                "model_name": registration.model,
                "context_window": registration.context_window,
                "temperature": registration.temperature,
                "provider_name": registration.provider,
                "source": f"model_config:{registration.id}",
                "vision_support": registration.vision_support,
                "max_tokens": registration.max_tokens,
                "enable_thinking": registration.enable_thinking,
                "encrypted_api_key": encrypted_api_key,
            },
        )

        self.session.add(llm)

        try:
            # Flush to get the ID, but don't commit
            await self.session.flush()
            await self.session.refresh(llm)

            logger.info(f"Created LLM entity: {llm.id} (model_id: {llm.model_id})")
            return llm

        except IntegrityError as e:
            logger.error(f"IntegrityError when creating LLM: {e.orig}")

            if "UniqueViolationError" in str(e.orig):
                raise ValueError(
                    f"Model ID '{llm_data.model_id}' already exists."
                ) from e
            else:
                raise ValueError(f"LLM creation failed: {e}") from e

    async def update_llm(
        self, llm_id: str, update_data: LlmModelCreate, tenant_id: str
    ) -> LlmModelEntity:
        """
        Update an existing LLM entity.
        Note: Caller is responsible for committing the session.

        Args:
            llm_id: LLM entity ID
            update_data: Updated LLM data

        Returns:
            Updated LlmModelEntity (not yet committed)

        Raises:
            ValueError: If LLM entity not found
        """
        result = await self.session.exec(select(LlmModelEntity).where(LlmModelEntity.id == llm_id, LlmModelEntity.tenant_id == tenant_id))
        llm = result.first()
        if not llm:
            raise ValueError(f"LLM '{llm_id}' does not exist.")

        logger.info(f"Updating LLM {llm_id} with data: {update_data}")

        model_id = update_data.model_id or llm.model_id
        registration = load_model_registry().llms.get(model_id)
        if registration is None:
            raise ValueError(
                f"LLM update failed: model_id '{model_id}' is not registered."
            )
        llm.model_id = registration.id
        llm.base_url = registration.base_url
        llm.model = registration.model
        llm.model_name = registration.model
        llm.context_window = registration.context_window
        llm.temperature = registration.temperature
        llm.provider_name = registration.provider
        llm.source = f"model_config:{registration.id}"
        llm.vision_support = registration.vision_support
        llm.enable_thinking = registration.enable_thinking
        llm.max_tokens = registration.max_tokens
        if update_data.api_key is not None:
            llm.encrypted_api_key = encrypt_key(update_data.api_key)
        if update_data.enabled is not None:
            llm.enabled = update_data.enabled

        self.session.add(llm)

        # Flush to ensure changes are staged
        await self.session.flush()
        await self.session.refresh(llm)

        logger.info(f"Updated LLM entity: {llm.id} (model_id: {llm.model_id})")
        return llm

    async def delete_llm(self, llm_id: str, tenant_id: str) -> None:
        """
        Delete an LLM entity.
        Note: Caller is responsible for committing the session.

        Args:
            llm_id: LLM entity ID

        Raises:
            ValueError: If LLM entity not found
        """
        result = await self.session.exec(select(LlmModelEntity).where(LlmModelEntity.id == llm_id, LlmModelEntity.tenant_id == tenant_id))
        llm = result.first()
        if not llm:
            raise ValueError(f"LLM '{llm_id}' does not exist.")

        # Delete from database (staged, not committed)
        await self.session.delete(llm)

        # Flush to ensure deletion is staged
        await self.session.flush()

        logger.info(f"Deleted LLM entity: {llm_id} (model_id: {llm.model_id})")

    async def get_all_llms(self, tenant_id: str) -> List[LlmModelEntity]:
        """
        Get all LLM entities without pagination.

        Returns:
            List of all LlmModelEntity
        """
        statement = select(LlmModelEntity).where(LlmModelEntity.tenant_id == tenant_id)
        results = await self.session.exec(statement)
        return list(results.all())

    async def get_llm_model_by_provider_model_id(self, provider_name: str, model_id: str, tenant_id: str) -> Optional[LlmModelEntity]:
        """
        Get a LLM entity by provider and model id.

        Args:
            provider_name: LLM provider name
            model_id: LLM model_id
            tenant_id: Tenant id

        Returns:
            LlmModelEntity if found, None otherwise
        """
        logger.info(f"Getting LLM model {model_id} by provider {provider_name} and tenant {tenant_id}.")
        # Preserve the legacy provider-agnostic lookup while allowing every
        # tenant to reuse the shared infrastructure model configuration.
        return await self.get_llm_by_model_id(model_id, tenant_id)
