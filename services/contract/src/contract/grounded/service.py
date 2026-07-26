from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contract.application.ports import InternalRequestContext
from contract.config import Settings
from contract.errors import ContractError
from contract.grounded.models import (
    GroundedAnswerData,
    GroundedChatRequest,
    GroundedReportRequest,
)


TASK_TYPE = "contract.grounded.answer"


class _FrameworkModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _TaskRecord(_FrameworkModel):
    id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    status: str = Field(min_length=1)
    input_payload_json: dict[str, Any]
    result_payload_json: dict[str, Any] | None = None
    error_payload_json: dict[str, Any] | None = None
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    current_run_id: str | None = None


class _TaskEnvelope(_FrameworkModel):
    task: _TaskRecord


class ContentMarkdownStreamDecoder:
    """Decode only the content_markdown JSON string from streamed model output."""

    _field = '"content_markdown"'
    _simple_escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def __init__(self) -> None:
        self._prefix = ""
        self._started = False
        self._finished = False
        self._escape = False
        self._unicode_digits: list[str] | None = None

    def feed(self, chunk: str) -> str:
        if not chunk or self._finished:
            return ""
        if not self._started:
            self._prefix += chunk
            value_start = self._find_value_start(self._prefix)
            if value_start is None:
                if len(self._prefix) > 100_000:
                    raise ValueError("content_markdown field was not found in streamed JSON")
                return ""
            chunk = self._prefix[value_start:]
            self._prefix = ""
            self._started = True

        decoded: list[str] = []
        for char in chunk:
            if self._unicode_digits is not None:
                if char not in "0123456789abcdefABCDEF":
                    raise ValueError("invalid Unicode escape in content_markdown")
                self._unicode_digits.append(char)
                if len(self._unicode_digits) == 4:
                    decoded.append(chr(int("".join(self._unicode_digits), 16)))
                    self._unicode_digits = None
                    self._escape = False
                continue
            if self._escape:
                if char == "u":
                    self._unicode_digits = []
                    continue
                mapped = self._simple_escapes.get(char)
                if mapped is None:
                    raise ValueError("invalid JSON escape in content_markdown")
                decoded.append(mapped)
                self._escape = False
                continue
            if char == "\\":
                self._escape = True
                continue
            if char == '"':
                self._finished = True
                break
            decoded.append(char)
        return "".join(decoded)

    @classmethod
    def _find_value_start(cls, value: str) -> int | None:
        field_index = value.find(cls._field)
        if field_index < 0:
            return None
        index = field_index + len(cls._field)
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value) or value[index] != ":":
            return None
        index += 1
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value) or value[index] != '"':
            return None
        return index + 1


class FrameworkGroundedAnswerService:
    """Run the registered grounded-answer Task through the Framework HTTP boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = settings.framework_base_url.rstrip("/")
        self.connect_timeout = settings.framework_connect_timeout_seconds
        self.read_timeout = settings.grounded_answer_timeout_seconds
        self.transport = transport

    async def generate_report(
        self,
        *,
        review_id: str,
        request: GroundedReportRequest,
        context: InternalRequestContext,
    ) -> GroundedAnswerData:
        payload: dict[str, Any] = {
            "schema_version": request.schema_version,
            "mode": "REPORT",
            "review_id": review_id,
            "document_id": request.document_id,
            "instruction": request.instruction,
            "question": None,
            "conversation_history": [],
        }
        return await self._execute(payload=payload, mode="REPORT", context=context)

    async def answer_chat(
        self,
        *,
        review_id: str,
        request: GroundedChatRequest,
        context: InternalRequestContext,
    ) -> GroundedAnswerData:
        payload: dict[str, Any] = {
            "schema_version": request.schema_version,
            "mode": "CHAT",
            "review_id": review_id,
            "document_id": request.document_id,
            "instruction": None,
            "question": request.question,
            "conversation_history": [
                item.model_dump(mode="json") for item in request.conversation_history
            ],
        }
        return await self._execute(payload=payload, mode="CHAT", context=context)

    async def stream_chat(
        self,
        *,
        review_id: str,
        request: GroundedChatRequest,
        context: InternalRequestContext,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        payload: dict[str, Any] = {
            "schema_version": request.schema_version,
            "mode": "CHAT",
            "review_id": review_id,
            "document_id": request.document_id,
            "instruction": None,
            "question": request.question,
            "conversation_history": [
                item.model_dump(mode="json") for item in request.conversation_history
            ],
        }
        async for event in self._stream_execute(
            payload=payload,
            mode="CHAT",
            context=context,
        ):
            yield event

    async def _execute(
        self,
        *,
        payload: dict[str, Any],
        mode: Literal["REPORT", "CHAT"],
        context: InternalRequestContext,
    ) -> GroundedAnswerData:
        if not context.idempotency_key:
            raise ContractError(
                "INVALID_REQUEST",
                "Idempotency-Key不能为空",
                status_code=400,
                user_action_required=True,
            )
        task_key = self._task_key(
            mode=mode,
            review_id=payload["review_id"],
            client_key=context.idempotency_key,
        )
        headers = {
            "X-Internal-Service": "ai-contract",
            "X-Tenant-Id": context.tenant_id,
            "X-User-Id": context.user_id,
            "X-Roles": "service",
        }
        task_body = {
            "task_type": TASK_TYPE,
            "title": f"Contract grounded {mode.lower()} {payload['review_id']}",
            "input_payload": payload,
            "stream": False,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "idempotency_key": task_key,
            "metadata": {
                "source_service": "ai-contract",
                "contract_review_id": payload["review_id"],
                "grounded_mode": mode,
                "schema_version": payload["schema_version"],
            },
        }
        created = await self._request(
            "POST",
            "/task-manager/tasks",
            headers={**headers, "Idempotency-Key": task_key},
            json=task_body,
        )
        task = self._validate_task(created, payload, context)
        if task.status == "running":
            task = await self._wait_for_task(
                task_id=task.id,
                headers=headers,
                payload=payload,
                context=context,
            )
        elif task.status in {"created", "pending"}:
            run_key = f"{task_key}:run"
            completed = await self._request(
                "POST",
                f"/task-manager/tasks/{task.id}/run",
                headers=headers,
                json={
                    "stream": False,
                    "idempotency_key": run_key,
                },
            )
            task = self._validate_task(completed, payload, context)
        elif task.status not in {"succeeded", "failed", "cancelled"}:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                f"Framework返回未知任务状态：{task.status}",
                status_code=502,
            )
        if task.status != "succeeded" or task.result_payload_json is None:
            details = task.error_payload_json or {"framework_status": task.status}
            raise ContractError(
                "GROUNDED_ANSWER_FAILED",
                "合同报告或问答生成失败",
                status_code=502,
                retryable=True,
                details=details,
            )
        structured_result = task.result_payload_json.get("structured")
        if not isinstance(structured_result, dict):
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework合同报告或问答结果缺少structured对象",
                status_code=502,
            )
        try:
            result = GroundedAnswerData.model_validate(structured_result)
        except ValidationError as exc:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework返回的合同报告或问答结构无效",
                status_code=502,
            ) from exc
        if (
            result.mode != mode
            or result.review_id != payload["review_id"]
            or result.document_id != payload["document_id"]
        ):
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework返回的合同报告或问答标识不一致",
                status_code=502,
            )
        return result

    async def _stream_execute(
        self,
        *,
        payload: dict[str, Any],
        mode: Literal["REPORT", "CHAT"],
        context: InternalRequestContext,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if not context.idempotency_key:
            raise ContractError(
                "INVALID_REQUEST",
                "Idempotency-Key不能为空",
                status_code=400,
                user_action_required=True,
            )
        task_key = self._task_key(
            mode=mode,
            review_id=payload["review_id"],
            client_key=context.idempotency_key,
        )
        headers = {
            "X-Internal-Service": "ai-contract",
            "X-Tenant-Id": context.tenant_id,
            "X-User-Id": context.user_id,
            "X-Roles": "service",
        }
        task_body = {
            "task_type": TASK_TYPE,
            "title": f"Contract grounded {mode.lower()} {payload['review_id']}",
            "input_payload": payload,
            "stream": True,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "idempotency_key": task_key,
            "metadata": {
                "source_service": "ai-contract",
                "contract_review_id": payload["review_id"],
                "grounded_mode": mode,
                "schema_version": payload["schema_version"],
            },
        }
        created = await self._request(
            "POST",
            "/task-manager/tasks",
            headers={**headers, "Idempotency-Key": task_key},
            json=task_body,
        )
        task = self._validate_task(created, payload, context)
        yield (
            "meta",
            {
                "schema_version": "1.0",
                "request_id": context.request_id,
                "task_id": task.id,
                "review_id": payload["review_id"],
                "document_id": payload["document_id"],
                "mode": mode,
                "reused": task.status != "created",
            },
        )

        if task.status == "succeeded":
            result = self._result_from_task(task, payload=payload, mode=mode)
            yield "snapshot", {"answer": result.model_dump(mode="json")}
            yield "done", {"answer": result.model_dump(mode="json")}
            return
        if task.status in {"failed", "cancelled"}:
            self._result_from_task(task, payload=payload, mode=mode)
            return

        decoder = ContentMarkdownStreamDecoder()
        if task.status in {"created", "pending"}:
            stream_path = f"/task-manager/tasks/{task.id}/stream"
            stream_method = "POST"
            stream_json = {
                "stream": True,
                "idempotency_key": f"{task_key}:run",
            }
        elif task.status == "running" and task.current_run_id:
            stream_path = (
                f"/task-manager/runs/{task.current_run_id}/events/stream"
            )
            stream_method = "GET"
            stream_json = None
        else:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                f"Framework返回无法流式续接的任务状态：{task.status}",
                status_code=502,
            )

        async for framework_event, envelope in self._stream_request(
            stream_method,
            stream_path,
            headers=headers,
            json_body=stream_json,
        ):
            payload_value = envelope.get("payload")
            event_payload = payload_value if isinstance(payload_value, dict) else {}
            sequence = envelope.get("sequence")
            run_id = envelope.get("run_id")
            event_type = str(envelope.get("event_type") or framework_event)
            if event_type.startswith("tool_"):
                yield (
                    "tool",
                    {
                        "event_type": event_type,
                        "run_id": run_id,
                        "sequence": sequence,
                        "stage_id": envelope.get("stage_id"),
                        "tool_call_id": envelope.get("tool_call_id"),
                        "payload": event_payload,
                    },
                )
            delta = event_payload.get("delta")
            if isinstance(delta, str) and delta:
                try:
                    content_delta = decoder.feed(delta)
                except ValueError as exc:
                    raise ContractError(
                        "FRAMEWORK_PROTOCOL_ERROR",
                        "Framework流式回答中的content_markdown无法解析",
                        status_code=502,
                    ) from exc
                if content_delta:
                    yield (
                        "delta",
                        {
                            "delta": content_delta,
                            "run_id": run_id,
                            "sequence": sequence,
                        },
                    )

        current = await self._request(
            "GET",
            f"/task-manager/tasks/{task.id}",
            headers=headers,
        )
        task = self._validate_task(current, payload, context)
        result = self._result_from_task(task, payload=payload, mode=mode)
        yield "done", {"answer": result.model_dump(mode="json")}

    async def _stream_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    method,
                    path,
                    headers=headers,
                    json=json_body,
                ) as response:
                    if response.status_code >= 500:
                        raise ContractError(
                            "FRAMEWORK_UNAVAILABLE",
                            f"Framework返回HTTP {response.status_code}",
                            status_code=503,
                            retryable=True,
                        )
                    if response.status_code >= 400:
                        raise ContractError(
                            "FRAMEWORK_PROTOCOL_ERROR",
                            f"Framework拒绝合同问答流式请求：HTTP {response.status_code}",
                            status_code=502,
                        )
                    event_name = "message"
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if not line:
                            if data_lines:
                                raw_data = "\n".join(data_lines)
                                try:
                                    value = json.loads(raw_data)
                                except ValueError as exc:
                                    raise ContractError(
                                        "FRAMEWORK_PROTOCOL_ERROR",
                                        "Framework SSE事件包含非法JSON",
                                        status_code=502,
                                    ) from exc
                                if not isinstance(value, dict):
                                    raise ContractError(
                                        "FRAMEWORK_PROTOCOL_ERROR",
                                        "Framework SSE事件必须为JSON对象",
                                        status_code=502,
                                    )
                                yield event_name, value
                            event_name = "message"
                            data_lines = []
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                    if data_lines:
                        value = json.loads("\n".join(data_lines))
                        if isinstance(value, dict):
                            yield event_name, value
        except httpx.TimeoutException as exc:
            raise ContractError(
                "FRAMEWORK_TIMEOUT",
                "合同问答流式生成超时",
                status_code=504,
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ContractError(
                "FRAMEWORK_UNAVAILABLE",
                "无法连接Framework流式接口",
                status_code=503,
                retryable=True,
            ) from exc

    @staticmethod
    def _result_from_task(
        task: _TaskRecord,
        *,
        payload: dict[str, Any],
        mode: Literal["REPORT", "CHAT"],
    ) -> GroundedAnswerData:
        if task.status != "succeeded" or task.result_payload_json is None:
            details = task.error_payload_json or {"framework_status": task.status}
            raise ContractError(
                "GROUNDED_ANSWER_FAILED",
                "合同报告或问答生成失败",
                status_code=502,
                retryable=True,
                details=details,
            )
        structured_result = task.result_payload_json.get("structured")
        if not isinstance(structured_result, dict):
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework合同报告或问答结果缺少structured对象",
                status_code=502,
            )
        try:
            result = GroundedAnswerData.model_validate(structured_result)
        except ValidationError as exc:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework返回的合同报告或问答结构无效",
                status_code=502,
            ) from exc
        if (
            result.mode != mode
            or result.review_id != payload["review_id"]
            or result.document_id != payload["document_id"]
        ):
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework返回的合同报告或问答标识不一致",
                status_code=502,
            )
        return result

    async def _wait_for_task(
        self,
        *,
        task_id: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        context: InternalRequestContext,
    ) -> _TaskRecord:
        deadline = asyncio.get_running_loop().time() + self.read_timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            current = await self._request(
                "GET",
                f"/task-manager/tasks/{task_id}",
                headers=headers,
            )
            task = self._validate_task(current, payload, context)
            if task.status != "running":
                return task
        raise ContractError(
            "FRAMEWORK_TIMEOUT",
            "等待合同报告或问答任务完成超时",
            status_code=504,
            retryable=True,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise ContractError(
                "FRAMEWORK_TIMEOUT",
                "合同报告或问答生成超时",
                status_code=504,
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ContractError(
                "FRAMEWORK_UNAVAILABLE",
                "无法连接Framework",
                status_code=503,
                retryable=True,
            ) from exc
        if response.status_code >= 500:
            raise ContractError(
                "FRAMEWORK_UNAVAILABLE",
                f"Framework返回HTTP {response.status_code}",
                status_code=503,
                retryable=True,
            )
        if response.status_code >= 400:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                f"Framework拒绝合同报告或问答请求：HTTP {response.status_code}",
                status_code=502,
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework返回非JSON内容",
                status_code=502,
            ) from exc
        if not isinstance(value, dict):
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework响应必须为JSON对象",
                status_code=502,
            )
        return value

    @staticmethod
    def _validate_task(
        value: dict[str, Any],
        payload: dict[str, Any],
        context: InternalRequestContext,
    ) -> _TaskRecord:
        try:
            task = _TaskEnvelope.model_validate(value).task
        except ValidationError as exc:
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework任务响应结构无效",
                status_code=502,
            ) from exc
        if (
            task.task_type != TASK_TYPE
            or task.tenant_id != context.tenant_id
            or task.user_id != context.user_id
        ):
            raise ContractError(
                "FRAMEWORK_PROTOCOL_ERROR",
                "Framework复用了不兼容的合同报告或问答任务",
                status_code=502,
            )
        if task.input_payload_json != {
            key: value for key, value in payload.items() if value is not None
        }:
            raise ContractError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key已用于不同的合同报告或问答请求",
                status_code=409,
                user_action_required=True,
            )
        return task

    @staticmethod
    def _task_key(*, mode: str, review_id: str, client_key: str) -> str:
        digest = hashlib.sha256(
            f"{mode}\n{review_id}\n{client_key}".encode("utf-8")
        ).hexdigest()
        return f"contract-grounded:{mode.lower()}:{digest}"
