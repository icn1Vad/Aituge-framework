import asyncio
from types import SimpleNamespace

from task_manager.api import _STREAM_DONE, _run_task_to_queue
from task_manager.handlers.scheduler_task import _translate_chunk_event
from task_manager.schemas import TaskRunRequest
from task_manager.service import MAX_EVENT_PAYLOAD_CHARS, _bounded_event_payload


def _event(data):
    return SimpleNamespace(data=data, thread_id="thread-1", session_id="session-1")


def test_reasoning_and_cumulative_citations_are_not_forwarded():
    data = {
        "choices": [{"delta": {"content": "", "reasoning_content": "internal"}}],
        "citations": [{"content": "x" * 50_000}],
        "citation_details": [{"content": "y" * 50_000}],
    }

    assert sum(len(_translate_chunk_event(_event(data))) for _ in range(3_000)) == 0


def test_tool_events_are_compact_and_do_not_include_results():
    action = {
        "id": "call-1",
        "function": {"name": "aliyun-websearch", "arguments": '{"query":"policy"}'},
    }
    started = _translate_chunk_event(_event({"actions": [action]}))
    completed = _translate_chunk_event(
        _event(
            {
                "observation": {
                    "tool": action,
                    "result": "large search result" * 20_000,
                    "error": None,
                }
            }
        )
    )

    assert started[0].event_type == "tool_started"
    assert completed[0].event_type == "tool_completed"
    assert completed[0].payload["result_chars"] > 100_000
    assert "result" not in completed[0].payload
    assert len(str(completed[0].payload)) < 500


def test_event_payload_has_a_hard_size_limit():
    payload = _bounded_event_payload({"raw": "x" * (MAX_EVENT_PAYLOAD_CHARS * 4)})

    assert payload["truncated"] is True
    assert payload["original_chars"] > MAX_EVENT_PAYLOAD_CHARS
    assert len(payload["preview"]) <= MAX_EVENT_PAYLOAD_CHARS // 2


def test_background_run_finishes_without_an_active_consumer():
    class FakeService:
        finished = False

        async def stream_task(self, task_id, request):
            for sequence in range(3):
                await asyncio.sleep(0)
                yield SimpleNamespace(sequence=sequence)
            self.finished = True

    async def run():
        service = FakeService()
        queue = asyncio.Queue()
        producer = asyncio.create_task(
            _run_task_to_queue(service, "task-1", TaskRunRequest(stream=True), queue)
        )

        await queue.get()
        await producer

        assert service.finished is True
        assert queue.qsize() == 3
        assert await queue.get() is not _STREAM_DONE
        assert await queue.get() is not _STREAM_DONE
        assert await queue.get() is _STREAM_DONE

    asyncio.run(run())
