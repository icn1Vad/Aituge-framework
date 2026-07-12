from __future__ import annotations

from task_manager.service import _json_safe_payload


def test_json_safe_payload_converts_nested_validation_exception():
    payload = {
        "errors": [{"type": "value_error", "ctx": {"error": ValueError("bad date")}}],
        "items": (ValueError("bad item"),),
    }
    safe = _json_safe_payload(payload)
    assert safe["errors"][0]["ctx"]["error"] == {
        "type": "ValueError",
        "message": "bad date",
    }
    assert safe["items"][0]["message"] == "bad item"
