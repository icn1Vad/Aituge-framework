import pytest

from task_manager.handlers.scheduler_task import _build_task_message
from task_manager.models import TaskEntity
from task_manager.payload_schemas import validate_input_payload
from task_manager.registry import get_task_definition


def test_media_topic_search_accepts_explicit_hotspot_mode():
    payload = validate_input_payload(
        "media_topic_search_input",
        {
            "message": "帮我找今天和近 7 天的热点",
            "search_mode": "hotspot_discovery",
        },
    )

    assert payload["search_mode"] == "hotspot_discovery"


def test_media_topic_search_rejects_unknown_mode():
    with pytest.raises(ValueError, match="media_topic_search_input"):
        validate_input_payload(
            "media_topic_search_input",
            {"message": "热点", "search_mode": "autonomous"},
        )


def test_media_topic_search_prompt_locks_requested_mode():
    task = TaskEntity(
        task_type="media.topic.search",
        title="热点发现",
        input_payload_json={
            "message": "帮我找今天和近 7 天的热点",
            "search_mode": "hotspot_discovery",
        },
    )

    prompt = _build_task_message(task, get_task_definition("media.topic.search"))

    assert "Requested search mode: hotspot_discovery" in prompt
    assert "query_plan.mode must equal it exactly" in prompt
    assert "broad current-hotspot discovery" in prompt
