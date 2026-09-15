import os
import uuid

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("AITUGE_RUN_LIVE_TESTS") != "1",
    reason="Set AITUGE_RUN_LIVE_TESTS=1 to run against explicitly configured live services.",
)


APPLIANCE_BASE_URL = os.getenv("AITUGE_APPLIANCE_BASE_URL", "http://127.0.0.1:8894")
SMOKE_BASE_URL = os.getenv("AITUGE_SMOKE_BASE_URL", "http://127.0.0.1:18200")


def test_live_model_calls_tool_from_second_capability_entry():
    sentinel = f"multi-capability-{uuid.uuid4().hex}"

    with httpx.Client(timeout=240) as client:
        smoke_health = client.get(f"{SMOKE_BASE_URL}/health")
        assert smoke_health.status_code == 200, smoke_health.text

        capabilities = client.get(f"{APPLIANCE_BASE_URL}/registry/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        capability_names = {
            item["name"] for item in capabilities.json()["capabilities"]
        }
        assert "proof_search" in capability_names
        assert "smoke_echo" in capability_names

        definitions = client.get(f"{APPLIANCE_BASE_URL}/task-manager/definitions")
        assert definitions.status_code == 200, definitions.text
        task_types = {
            item["task_type"] for item in definitions.json()["definitions"]
        }
        assert "proof.qa.chat" in task_types
        assert "smoke.echo.chat" in task_types

        response = client.post(
            f"{APPLIANCE_BASE_URL}/task-manager/run",
            json={
                "task_type": "smoke.echo.chat",
                "stream": False,
                "input_payload": {"message": sentinel},
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["status"] == "succeeded", body
    tool_events = {
        (event["event_type"], event.get("payload_json", {}).get("tool_name"))
        for event in body["events"]
    }
    assert ("tool_started", "smoke_echo") in tool_events
    assert ("tool_completed", "smoke_echo") in tool_events
    content = body["task"]["result_payload_json"]["content"]
    assert sentinel in content
    assert "smoke-service" in content
