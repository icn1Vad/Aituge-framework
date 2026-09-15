"""Framework dependencies for the migrated PAI-RAG file services."""
from enum import Enum
from fastapi import Depends, Header, HTTPException
from db.db_context import get_db_session
from common.system_constants import DEFAULT_TENANT_ID

class FileStatus(str, Enum):
    pending = "pending"
    parsing = "parsing"
    persisting = "persisting"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"

class ApiException(HTTPException):
    def __init__(self, code, message):
        super().__init__(status_code=code, detail=message)
    @classmethod
    def not_found(cls, identifier, resource):
        return cls(404, f"{resource} {identifier} not found")

def success_response(data=None):
    return {"code": 200, "message": "success", "data": data}

async def get_tenant_id(x_tenant_id: str = Header(DEFAULT_TENANT_ID)):
    return x_tenant_id

async def get_file_resource_service(session=Depends(get_db_session)):
    from .service.file_resource_service import FileResourceService
    return FileResourceService(session)
