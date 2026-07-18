from __future__ import annotations

import asyncio

from db.db_context import create_db_session, init_db, reset_engine_for_test
from task_manager import item_store
from task_manager.models import TaskEntity


def test_pipeline_batch_stages_use_isolated_item_sets(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'items.db'}")
        reset_engine_for_test()
        await init_db()
        task = TaskEntity(id="task-1", task_type="proof.audit.run")
        async with create_db_session() as session:
            session.add(task)
            await session.commit()

        await asyncio.gather(
            item_store.ensure_stage_items(
                task.id,
                item_type="pipeline:semantic_audit",
                raw_items=[{"id": "semantic-1"}, {"id": "semantic-2"}],
            ),
            item_store.ensure_stage_items(
                task.id,
                item_type="pipeline:conflict_audit",
                raw_items=[{"id": "conflict-1"}, {"id": "conflict-2"}],
            ),
        )

        semantic = await item_store.list_pending_items(
            task.id, item_type="pipeline:semantic_audit"
        )
        conflict = await item_store.list_pending_items(
            task.id, item_type="pipeline:conflict_audit"
        )
        assert [item.item_key for item in semantic] == ["semantic-1", "semantic-2"]
        assert [item.item_key for item in conflict] == ["conflict-1", "conflict-2"]

        await item_store.finish_item_success(semantic[0].id, {"status": "succeeded"})
        conflict_results = await item_store.load_item_results(
            task.id, item_type="pipeline:conflict_audit"
        )
        assert all(item["status"] == "pending" for item in conflict_results)

        # Re-seeding a resumed stage is idempotent.
        await item_store.ensure_stage_items(
            task.id,
            item_type="pipeline:conflict_audit",
            raw_items=[{"id": "conflict-1"}, {"id": "conflict-2"}],
        )
        assert len(
            await item_store.load_item_results(
                task.id, item_type="pipeline:conflict_audit"
            )
        ) == 2

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
