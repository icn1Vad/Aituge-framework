from fastapi import APIRouter

def create_router():
    from .files import files_router
    from .files_uploads import files_uploads_router
    from .files_events import files_events_router
    from .references import references_router
    router = APIRouter(tags=["attachments"])
    router.include_router(files_uploads_router, prefix="/v1/files/uploads")
    router.include_router(files_events_router, prefix="/v1/files/events")
    router.include_router(references_router, prefix="/v1/files/references")
    router.include_router(files_router, prefix="/v1/files")
    return router
