from task_manager.models import TaskEventEntity


def test_task_event_defines_admin_query_indexes():
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in TaskEventEntity.__table__.indexes
    }

    assert indexes["idx_tuge_task_event_task_created"] == (
        "task_id",
        "created_at",
    )
    assert indexes["idx_tuge_task_event_type_created"] == (
        "event_type",
        "created_at",
    )
