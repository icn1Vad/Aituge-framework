import asyncio
from pathlib import Path
import httpx
from fastapi import FastAPI
from sqlmodel import select
from backend.attachments.api import create_router
from backend.attachments.tasks import configure
from db.db_context import init_db, create_db_session, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.models import TaskEntity
from task_manager.service import TaskManagerService


def test_upload_runs_through_task_manager_and_restores(tmp_path, monkeypatch):
    monkeypatch.setenv('SQLITE_URL', 'sqlite+aiosqlite:///' + str(tmp_path / 'files.db'))
    monkeypatch.setenv('AITUGE_ATTACHMENT_STORAGE_ROOT', str(tmp_path / 'files'))
    from backend.attachments.vendor.file.store.file_store_helper import get_file_store
    get_file_store.cache_clear()
    async def run():
        reset_engine_for_test()
        await init_db()
        options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / 'artifacts')
        configure(options)
        app = FastAPI(); app.include_router(create_router())
        headers={'X-Tenant-Id':'api-attachments'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
            response=await client.post('/v1/files',files={'file':('测试.txt','总金额 1250 元'.encode())},data={'purpose':'chat_attachment'})
            assert response.status_code == 202, response.text
            file_id=response.json()['data']['id']
            async with create_db_session() as session:
                task=(await session.exec(select(TaskEntity).where(TaskEntity.task_type=='attachments.parse'))).first()
            assert task is not None
            from task_manager.runtime.worker import TaskWorker
            worker = TaskWorker(options=options)
            assert await worker.run_once()
            finished=await TaskManagerService(options).get_task(task.id)
            assert finished.status == 'succeeded', finished.error_payload_json
            response=await client.get(f'/v1/files/{file_id}/text')
            assert '1250' in response.json()['data']['content']
            # Restart DB connections; neither file bytes nor extracted text are process memory.
            reset_engine_for_test()
            await init_db()
            response=await client.get(f'/v1/files/{file_id}/content')
            assert response.content == '总金额 1250 元'.encode()
            assert "filename*=UTF-8''" in response.headers['content-disposition']
            response=await client.get('/v1/files',params={'ids':file_id})
            assert response.json()['data'][0]['status']=='succeeded'
    asyncio.run(run())
    get_file_store.cache_clear()
