"""Message-owned sets, not increment commands: replay is idempotent."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlalchemy.exc import IntegrityError
from db.db_context import get_db_session
from ..models import FileEntity, FileReferenceOwner
from ..support import ApiException, get_tenant_id, success_response
from ..service.file_resource_service import FileResourceService

references_router = APIRouter()
class ReferenceSet(BaseModel):
    revision: int = Field(ge=1)
    file_ids: list[str] = Field(default_factory=list)

async def set_references(session, tenant_id, owner_id, request):
    # The unique owner key arbitrates concurrent first delivery. All later
    # revisions lock the ledger row before changing reference counts.
    owner = await session.get(FileReferenceOwner, (tenant_id, owner_id))
    if owner is None:
        session.add(FileReferenceOwner(tenant_id=tenant_id, owner_id=owner_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
    owner = (await session.exec(select(FileReferenceOwner).where(
        FileReferenceOwner.tenant_id == tenant_id, FileReferenceOwner.owner_id == owner_id).with_for_update())).one()
    if request.revision <= owner.revision:
        return {"owner_id": owner_id, "revision": owner.revision, "file_ids": owner.file_ids}
    old, new = set(owner.file_ids), set(request.file_ids)
    svc = FileResourceService(session)
    # Lock files too, so deletion cannot race a new reference.
    files = (await session.exec(select(FileEntity).where(FileEntity.tenant_id == tenant_id,
              FileEntity.id.in_(sorted(old | new))).order_by(FileEntity.id).with_for_update())).all()
    if new - {row.id for row in files}:
        raise ApiException(404, "部分附件不存在")
    await svc.increment_refs(sorted(new - old), tenant_id)
    await svc.decrement_refs(sorted(old - new), tenant_id)
    owner.file_ids = sorted(new)
    owner.revision = request.revision
    session.add(owner)
    await session.commit()
    return {"owner_id": owner_id, "revision": request.revision, "file_ids": sorted(new)}

@references_router.put("/{owner_id}")
async def put_references(owner_id: str, request: ReferenceSet,
                         session=Depends(get_db_session), tenant_id=Depends(get_tenant_id)):
    return success_response(data=await set_references(session, tenant_id, owner_id, request))
