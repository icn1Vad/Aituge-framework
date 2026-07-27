from __future__ import annotations

from dataclasses import dataclass

from common.system_constants import DEFAULT_TENANT_ID
from fastapi import Header, HTTPException

from .models import TaskEntity


@dataclass(frozen=True, slots=True)
class TaskAccessContext:
    user_id: str
    tenant_id: str
    roles: frozenset[str]
    service_name: str = "external"

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


async def task_access_context(
    x_user_id: str = Header(default="default_user"),
    x_tenant_id: str = Header(default=DEFAULT_TENANT_ID),
    x_roles: str = Header(default=""),
    x_internal_service: str = Header(default="external", alias="X-Internal-Service"),
) -> TaskAccessContext:
    roles = frozenset(role.strip() for role in x_roles.split(",") if role.strip())
    return TaskAccessContext(
        user_id=x_user_id,
        tenant_id=x_tenant_id,
        roles=roles,
        service_name=x_internal_service.strip() or "external",
    )


def assert_can_access_task(task: TaskEntity, context: TaskAccessContext) -> None:
    if context.is_admin:
        return
    if task.user_id != context.user_id or task.tenant_id != context.tenant_id:
        raise HTTPException(status_code=403, detail="Task access denied.")
