from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlmodel import select

from common.encrypt_utils import decrypt_key, encrypt_key
from db.db_context import create_db_session
from db.models.llm import LlmModelEntity
from service.conversation.llm_runner import LlmRuntime

from translation_service.config import Settings
from translation_service.errors import ModelCallError


class BaseModelGateway:
    """Calls Framework's OpenAI-compatible LLM runtime without an Agent."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._configured_tenants: set[str] = set()
        self._configuration_lock = asyncio.Lock()

    async def complete(
        self,
        *,
        tenant_id: str,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        await self._ensure_model(tenant_id)
        runtime = LlmRuntime(tenant_id=tenant_id)
        try:
            return await asyncio.wait_for(
                runtime.complete(
                    [{"role": "user", "content": user_content}],
                    model_id=self._settings.model_id,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens or self._settings.model_max_tokens,
                    temperature=(
                        self._settings.model_temperature
                        if temperature is None
                        else temperature
                    ),
                ),
                timeout=self._settings.model_call_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ModelCallError("Translation model call timed out") from exc
        except Exception as exc:
            raise ModelCallError() from exc

    async def _ensure_model(self, tenant_id: str) -> None:
        if tenant_id in self._configured_tenants:
            return
        async with self._configuration_lock:
            if tenant_id in self._configured_tenants:
                return
            async with create_db_session() as session:
                result = await session.exec(
                    select(LlmModelEntity).where(
                        LlmModelEntity.tenant_id == tenant_id,
                        LlmModelEntity.model_id == self._settings.model_id,
                    )
                )
                row = result.first()
                api_key = self._settings.model_api_key.get_secret_value()
                if row is None:
                    row = LlmModelEntity(
                        tenant_id=tenant_id,
                        base_url=self._settings.model_base_url,
                        model=self._settings.model_name,
                        model_name=self._settings.model_name,
                        model_id=self._settings.model_id,
                        enabled=True,
                        vision_support=False,
                        max_tokens=self._settings.model_max_tokens,
                        context_window=self._settings.model_context_window,
                        temperature=self._settings.model_temperature,
                        enable_thinking=False,
                        provider_name="openai_like",
                        source="translation_service",
                        encrypted_api_key=encrypt_key(api_key),
                    )
                else:
                    current_key = ""
                    try:
                        current_key = decrypt_key(row.encrypted_api_key or "")
                    except Exception:
                        current_key = ""
                    row.base_url = self._settings.model_base_url
                    row.model = self._settings.model_name
                    row.model_name = self._settings.model_name
                    row.enabled = True
                    row.max_tokens = self._settings.model_max_tokens
                    row.context_window = self._settings.model_context_window
                    row.temperature = self._settings.model_temperature
                    row.enable_thinking = False
                    row.provider_name = "openai_like"
                    row.source = "translation_service"
                    if current_key != api_key:
                        row.encrypted_api_key = encrypt_key(api_key)
                session.add(row)
                await session.commit()
            self._configured_tenants.add(tenant_id)


def parse_json_object(value: str) -> dict[str, Any]:
    candidate = value.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as whole_value_error:
        # OpenAI-compatible reasoning models can prepend a <think> block or
        # short prose even when instructed to return JSON only. Accept only a
        # complete, balanced top-level JSON object from that wrapper text.
        objects = _balanced_json_objects(candidate)
        for object_candidate in reversed(objects):
            try:
                parsed = json.loads(object_candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise ModelCallError("Translation model returned invalid structured data") from whole_value_error
    if not isinstance(parsed, dict):
        raise ModelCallError("Translation model did not return a JSON object")
    return parsed


def _balanced_json_objects(value: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(value[start : index + 1])
                start = None
    return objects
