# Java integration: Proof Policy Q&A Task SSE

## Addresses

- From containers on `agent-internal`: `http://ai-framework:8894`
- From the appliance host: `http://127.0.0.1:8894`
- Proof is an internal dependency of Framework. Java should call Framework,
  not orchestrate Proof directly.

## Stream request

```http
POST /task-manager/stream
Content-Type: application/json
Accept: text/event-stream
X-User-Id: <stable-user-id>
X-Tenant-Id: __default_tenant_id__
```

```json
{
  "task_type": "proof.qa.chat",
  "task_key": "proof-qa-<business-request-id>",
  "title": "制度问答",
  "stream": true,
  "input_payload": {
    "question": "关联交易需要履行哪些程序？",
    "top_k": 5
  },
  "metadata": {
    "source": "java-adapter"
  }
}
```

Use a stable business request ID in `task_key`. Keep the HTTP response open and
parse standard SSE blocks separated by a blank line:

```text
event: stream_chunk
data: {"task_id":"...","event_type":"stream_chunk","payload_json":{"delta":"..."}}
```

## Events Java needs

| Event | Meaning | Useful fields |
| --- | --- | --- |
| `task_created` | Task persisted | `task_id`, `run_id` |
| `task_started` | Execution started | `task_id`, `sequence` |
| `tool_started` | Tool call started | `payload_json.tool_name`, `arguments` |
| `tool_completed` | Tool call completed | `status`, `result_chars`, `error` |
| `stream_chunk` | Answer delta | `payload_json.delta` |
| `agent_final` | Model stream finished | `payload_json.content_chars`, token usage |
| `task_succeeded` | Terminal success | `task_id`, `run_id` |
| `task_failed` | Terminal failure | `error_code`, `message`, `payload_json` |

Concatenate `payload_json.delta` from `stream_chunk` events in sequence order.
Treat only `task_succeeded` and `task_failed` as terminal. HTTP 4xx/5xx means the
request was rejected before or while creating the stream; a successful HTTP 200
stream can still terminate with `task_failed`.

## Curl smoke test

```bash
curl -N --max-time 180 \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H 'X-User-Id: java-smoke' \
  -H 'X-Tenant-Id: __default_tenant_id__' \
  --data '{
    "task_type":"proof.qa.chat",
    "task_key":"proof-qa-java-smoke-001",
    "stream":true,
    "input_payload":{"question":"关联交易需要履行哪些程序？","top_k":5},
    "metadata":{"source":"java-adapter"}
  }' \
  http://ai-framework:8894/task-manager/stream
```

Configure a connection timeout around 5-10 seconds and a stream/read timeout of
at least 180 seconds. Do not buffer the entire response before forwarding answer
deltas to the frontend.
