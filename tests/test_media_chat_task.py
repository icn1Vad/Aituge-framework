from task_manager.handlers.scheduler_task import _build_task_message
from task_manager.models import TaskEntity
from task_manager.payload_schemas import validate_input_payload
from task_manager.registry import get_task_definition


def test_media_chat_reuses_scheduler_with_read_only_skill():
    definition = get_task_definition("media.chat")

    assert definition.handler == "scheduler"
    assert definition.default_agent_id == "default-single-agent"
    assert definition.default_skill_package == "media-script-chat-package"
    assert definition.default_primary_skill == "media-script-chat"
    assert definition.default_tools == []
    assert definition.default_datasets == []
    assert definition.output_schema_name is None


def test_media_chat_input_and_message_keep_bounded_script_context():
    payload = validate_input_payload(
        "media_chat_input",
        {
            "message": "Why was this persona selected?",
            "topic": "Veteran employment",
            "context": {
                "script_id": "script-1",
                "current_script": {"persona_name": "Yan Jie", "voiceover": "Current voiceover."},
                "review_result": {"summary": "Check the policy boundary."},
                "recent_messages": [{"role": "user", "content": "Previous question"}],
            },
        },
    )
    task = TaskEntity(
        id="task-1",
        task_type="media.chat",
        title="Script chat",
        input_payload_json=payload,
        user_id="user-1",
        tenant_id="tenant-1",
    )

    message = _build_task_message(task, get_task_definition("media.chat"))

    assert "read-only media script conversation assistant" in message
    assert "Why was this persona selected?" in message
    assert '"script_id": "script-1"' in message
    assert "Return a concise natural-language answer" in message
