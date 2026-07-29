from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from aituge_model_config import get_model_pack_for_ai_mode
from fastapi import Header, HTTPException

from .models import TaskEntity


@dataclass(frozen=True, slots=True)
class TaskAccessContext:
    user_id: str
    tenant_id: str
    roles: frozenset[str]
    service_name: str = "external"
    model_pack_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


async def task_access_context(
    x_user_id: str = Header(default="default_user"),
    x_tenant_id: str | None = Header(default=None),
    x_roles: str = Header(default=""),
    x_internal_service: str = Header(default="external", alias="X-Internal-Service"),
    x_ai_mode: Annotated[str | None, Header(alias="X-AI-Mode")] = None,
) -> TaskAccessContext:
    tenant_id = str(x_tenant_id or "").strip()
    if not tenant_id or tenant_id.lower() in {"null", "none", "undefined"}:
        raise HTTPException(status_code=400, detail="X-Tenant-Id is required.")
    roles = frozenset(role.strip() for role in x_roles.split(",") if role.strip())
    model_pack_id = _model_pack_for_mode(x_ai_mode)
    return TaskAccessContext(
        user_id=x_user_id,
        tenant_id=tenant_id,
        roles=roles,
        service_name=x_internal_service.strip() or "external",
        model_pack_id=model_pack_id,
    )


def _model_pack_for_mode(value: str | None) -> str | None:
    """Map the user-facing mode to a registered package without exposing IDs."""

    raw_mode = str(value or "").strip().lower()
    if not raw_mode:
        return None
    if raw_mode not in {"public", "private"}:
        raise HTTPException(status_code=400, detail="X-AI-Mode must be public or private.")
    try:
        return get_model_pack_for_ai_mode(raw_mode).id
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Model pack for {raw_mode} mode is not configured.",
        ) from exc


def assert_can_access_task(task: TaskEntity, context: TaskAccessContext) -> None:
    if context.is_admin:
        return
    if task.user_id != context.user_id or task.tenant_id != context.tenant_id:
        raise HTTPException(status_code=403, detail="Task access denied.")
