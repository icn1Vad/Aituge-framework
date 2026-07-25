from __future__ import annotations

import asyncio
import hashlib
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


class _TaskEnvelope(_FrameworkModel):
    task: _TaskRecord


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
