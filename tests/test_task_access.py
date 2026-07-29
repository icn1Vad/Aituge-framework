from __future__ import annotations

import pytest
from fastapi import HTTPException

from task_manager.access import TaskAccessContext, task_access_context
from task_manager.api import _scope_task_create_request
from task_manager.schemas import TaskCreateRequest


@pytest.mark.asyncio
async def test_task_access_requires_java_tenant_header() -> None:
    for tenant_id in (None, "", "null"):
        with pytest.raises(HTTPException) as exc_info:
            await task_access_context(
                x_user_id="java-user",
                x_tenant_id=tenant_id,
                x_roles="",
                x_internal_service="external",
                x_ai_mode=None,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "X-Tenant-Id is required."


@pytest.mark.asyncio
async def test_task_access_normalizes_java_tenant_header() -> None:
    context = await task_access_context(
        x_user_id="java-user",
        x_tenant_id=" 22 ",
        x_roles="",
        x_internal_service="external",
        x_ai_mode=None,
    )

    assert context.tenant_id == "22"
    assert context.model_pack_id is None


@pytest.mark.asyncio
async def test_task_access_maps_global_ai_modes_to_registered_packages() -> None:
    public = await task_access_context(
        x_user_id="public-user",
        x_tenant_id="22",
        x_roles="",
        x_internal_service="external",
        x_ai_mode="public",
    )
    private = await task_access_context(
        x_user_id="private-user",
        x_tenant_id="22",
        x_roles="",
        x_internal_service="external",
        x_ai_mode="private",
    )

    assert public.model_pack_id == "api-rerank"
    assert private.model_pack_id == "api-rerank-similarity"


@pytest.mark.asyncio
async def test_task_access_rejects_unknown_ai_mode() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await task_access_context(
            x_user_id="java-user",
            x_tenant_id="22",
            x_roles="",
            x_internal_service="external",
            x_ai_mode="arbitrary-package",
        )

    assert exc_info.value.status_code == 400


def test_ai_mode_package_overrides_untrusted_task_body_package() -> None:
    request = TaskCreateRequest(
        task_type="proof.qa.chat",
        input_payload={"question": "test"},
        model_pack_id="api-rerank",
    )
    context = TaskAccessContext(
        user_id="java-user",
        tenant_id="22",
        roles=frozenset(),
        model_pack_id="api-rerank-similarity",
    )

    scoped = _scope_task_create_request(request, context)

    assert scoped.user_id == "java-user"
    assert scoped.tenant_id == "22"
    assert scoped.model_pack_id == "api-rerank-similarity"
