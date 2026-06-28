"""PAI-style limited remote code sandbox client.

This module is intentionally close to PAI-RAG's CodeSandboxTool flow:

1. create a sandbox instance
2. wait for health status `ok`
3. create a Python context
4. execute Python code or package install commands
5. clean up the sandbox instance

The implementation is "limited" in name and behavior: it exposes only Python
execution and package installation, applies default timeouts, truncates output,
and does not embed Hermes-style nested tool calling.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Union

import aiohttp
from loguru import logger

from .limited_code_sandbox_exceptions import (
    LimitedCodeSandboxAPIException,
    LimitedCodeSandboxEmptyCodeException,
    LimitedCodeSandboxExecutionException,
    LimitedCodeSandboxHTTPException,
    LimitedCodeSandboxNotConfiguredException,
    LimitedCodeSandboxNotInitializedException,
    LimitedCodeSandboxTimeoutException,
)


TRIPLE_QUOTE_PATTERN = re.compile(r"```[^\n]*\n(.+?)```", re.DOTALL)
XML_CODE_PATTERN = re.compile(r"<code>(.*?)</code>", re.DOTALL)


@dataclass(slots=True)
class LimitedCodeSandboxConfig:
    """Config for the limited Aliyun-FC-compatible code sandbox.

    `base_url` may be provided directly for tests or non-Aliyun compatible
    deployments. If it is empty, `aliyun_id` is used to build PAI's default
    endpoint: `https://{aliyun_id}.agentrun-data.cn-hangzhou.aliyuncs.com`.
    """

    enabled: bool = False
    aliyun_id: str = ""
    interpreter_id: str = ""
    interpreter_name: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout_default: int = 50
    health_max_wait_seconds: int = 60
    max_output_chars: int = 50_000


class LimitedCodeSandboxTool:
    """Limited PAI-style code sandbox client."""

    def __init__(self, config: LimitedCodeSandboxConfig):
        self.config = config
        self.enabled = config.enabled
        self.aliyun_id = config.aliyun_id
        self.interpreter_id = config.interpreter_id
        self.interpreter_name = config.interpreter_name
        self.timeout_default = config.timeout_default
        self.api_key = config.api_key
        self.base_url = (config.base_url or self._build_aliyun_base_url(config.aliyun_id)).rstrip("/")
        self._sandbox_initialized = False
        self._sandbox_id: Optional[str] = None
        self._sandbox_context_id: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def _build_aliyun_base_url(aliyun_id: str) -> str:
        if not aliyun_id:
            return ""
        return f"https://{aliyun_id}.agentrun-data.cn-hangzhou.aliyuncs.com"

    def _validate_configured(self) -> None:
        if not self.enabled:
            raise LimitedCodeSandboxNotConfiguredException("Limited code sandbox is disabled.")
        if not self.base_url:
            raise LimitedCodeSandboxNotConfiguredException(
                "Limited code sandbox requires base_url or aliyun_id."
            )
        if not self.interpreter_name:
            raise LimitedCodeSandboxNotConfiguredException(
                "Limited code sandbox requires interpreter_name."
            )

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=max(self.timeout_default, 30) + 30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ensure_sandbox_initialized(self) -> None:
        self._validate_configured()
        if self._sandbox_initialized:
            return

        try:
            self._sandbox_id = await self.acreate_sandbox_instance()
            await self.acheck_sandbox_health(
                self._sandbox_id,
                max_wait_seconds=self.config.health_max_wait_seconds,
            )
            self._sandbox_context_id = await self.acreate_context(self._sandbox_id)
            self._sandbox_initialized = True
            logger.info("Limited code sandbox initialized successfully.")
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            logger.exception("Failed to initialize limited code sandbox.")
            raise LimitedCodeSandboxNotInitializedException(
                f"Failed to initialize limited code sandbox: {exc}"
            ) from exc

    async def acreate_sandbox_instance(self) -> str:
        self._validate_configured()
        session = await self._get_session()
        payload = {"templateName": self.interpreter_name}
        url = f"{self.base_url}/sandboxes"

        try:
            async with session.post(url, headers=self._get_headers(), json=payload) as response:
                if not response.ok:
                    text = await response.text()
                    raise LimitedCodeSandboxHTTPException(f"HTTP {response.status}: {text}")
                result = await response.json()
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            raise LimitedCodeSandboxAPIException(
                f"Failed to create sandbox instance: {exc}"
            ) from exc

        sandbox_id = (result.get("data") or {}).get("sandboxId")
        if not sandbox_id:
            raise LimitedCodeSandboxAPIException(
                f"Failed to create sandbox instance: invalid response {result!r}"
            )
        return str(sandbox_id)

    async def _fetch_sandbox_health_status(self, sandbox_id: str) -> str:
        session = await self._get_session()
        url = f"{self.base_url}/sandboxes/{sandbox_id}/health"

        try:
            async with session.get(url, headers=self._get_headers()) as response:
                text = await response.text()
                if not response.ok:
                    raise LimitedCodeSandboxHTTPException(f"HTTP {response.status}: {text}")
                result = json.loads(text) if text else {}
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            raise LimitedCodeSandboxAPIException(
                f"Failed to check sandbox health: {exc}"
            ) from exc

        status = result.get("status")
        if not status:
            raise LimitedCodeSandboxAPIException(
                f"Invalid health check response: {result!r}"
            )
        return str(status)

    async def acheck_sandbox_health(
        self,
        sandbox_id: str,
        max_wait_seconds: int = 60,
    ) -> str:
        try:
            async with asyncio.timeout(max_wait_seconds):
                while True:
                    status = await self._fetch_sandbox_health_status(sandbox_id)
                    if status == "ok":
                        return status
                    logger.info(
                        f"Limited code sandbox not ready (status={status}), waiting 5s."
                    )
                    await asyncio.sleep(5)
        except TimeoutError as exc:
            raise LimitedCodeSandboxTimeoutException(
                f"Sandbox health check timeout after {max_wait_seconds}s"
            ) from exc

    async def acreate_context(self, sandbox_id: str, cwd: Optional[str] = None) -> str:
        session = await self._get_session()
        payload: dict[str, Any] = {"language": "python"}
        if cwd:
            payload["cwd"] = cwd
        url = f"{self.base_url}/sandboxes/{sandbox_id}/contexts"

        try:
            async with session.post(url, headers=self._get_headers(), json=payload) as response:
                text = await response.text()
                if not response.ok:
                    raise LimitedCodeSandboxHTTPException(f"HTTP {response.status}: {text}")
                result = json.loads(text) if text else {}
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            raise LimitedCodeSandboxAPIException(f"Failed to create context: {exc}") from exc

        context_id = (result.get("data") or {}).get("id") or result.get("id")
        if not context_id:
            raise LimitedCodeSandboxAPIException(
                f"Failed to create context: invalid response {result!r}"
            )
        return str(context_id)

    async def adelete_sandbox_instance(self, sandbox_id: Optional[str] = None) -> Optional[dict]:
        sandbox_id = sandbox_id or self._sandbox_id
        if not sandbox_id:
            await self.aclose()
            return None

        session = await self._get_session()
        url = f"{self.base_url}/sandboxes/{sandbox_id}"
        try:
            async with session.delete(url, headers=self._get_headers()) as response:
                text = await response.text()
                if not response.ok:
                    raise LimitedCodeSandboxHTTPException(f"HTTP {response.status}: {text}")
                result = json.loads(text) if text else {}
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            raise LimitedCodeSandboxAPIException(
                f"Failed to delete sandbox instance: {exc}"
            ) from exc
        finally:
            self._sandbox_id = None
            self._sandbox_context_id = None
            self._sandbox_initialized = False
            await self.aclose()

        return result

    def _extract_code(self, params: Union[str, dict]) -> str:
        try:
            if isinstance(params, str):
                stripped = params.strip()
                if stripped.startswith("{"):
                    params = json.loads(stripped)
                else:
                    params = {"code": stripped}
            code = params.get("code", "") or params.get("raw", "")
            triple_match = TRIPLE_QUOTE_PATTERN.search(code)
            if triple_match:
                code = triple_match.group(1)
            else:
                xml_match = XML_CODE_PATTERN.search(code)
                if xml_match:
                    code = xml_match.group(1)
            return str(code).strip()
        except Exception:
            if isinstance(params, str):
                return params.strip()
            return ""

    def _truncate_output(self, text: str) -> str:
        limit = self.config.max_output_chars
        if limit <= 0 or len(text) <= limit:
            return text
        omitted = len(text) - limit
        return f"{text[:limit]}\n\n[LimitedCodeSandbox truncated {omitted} characters]"

    async def aexecute(
        self,
        code: Union[str, dict],
        timeout: Optional[int] = None,
        sandbox_id: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> str:
        code_text = self._extract_code(code)
        if not code_text:
            raise LimitedCodeSandboxEmptyCodeException("Empty or invalid code provided.")

        await self._ensure_sandbox_initialized()
        sandbox_id = sandbox_id or self._sandbox_id
        context_id = context_id or self._sandbox_context_id
        if not sandbox_id or not context_id:
            raise LimitedCodeSandboxNotInitializedException(
                "Sandbox or context not initialized."
            )

        payload = {"code": code_text, "timeout": timeout or self.timeout_default}
        url = f"{self.base_url}/sandboxes/{sandbox_id}/contexts/{context_id}/execute"
        session = await self._get_session()

        try:
            async with session.post(url, headers=self._get_headers(), json=payload) as response:
                response_text = await response.text()
                if not response.ok:
                    raise LimitedCodeSandboxHTTPException(
                        f"HTTP {response.status}: {response_text}"
                    )
                result = json.loads(response_text) if response_text else {}
        except asyncio.TimeoutError as exc:
            raise LimitedCodeSandboxTimeoutException("Execution timed out.") from exc
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            raise LimitedCodeSandboxExecutionException(
                f"Error during code execution: {exc}"
            ) from exc

        return self._format_execute_result(result)

    def _format_execute_result(self, result: dict[str, Any]) -> str:
        results = result.get("results", [])
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        error_lines: list[str] = []
        result_lines: list[str] = []
        timed_out = False

        for item in results:
            typ = item.get("type")
            if typ == "stdout":
                stdout_lines.append(item.get("text", ""))
            elif typ == "result":
                result_lines.append(item.get("text", ""))
            elif typ == "stderr":
                stderr_lines.append(item.get("text", ""))
            elif typ == "error":
                error_lines.append(f"{item.get('name', '')}: {item.get('value', '')}")
            elif typ == "timeout":
                timed_out = True

        if timed_out or any("TimeoutError" in line for line in stderr_lines):
            raise LimitedCodeSandboxTimeoutException("Execution timed out.")

        parts = []
        if stdout_lines:
            parts.append("stdout:\n" + "".join(stdout_lines).rstrip())
        if result_lines:
            parts.append("result:\n" + "".join(result_lines).rstrip())
        if stderr_lines:
            parts.append("stderr:\n" + "".join(stderr_lines).rstrip())
        if error_lines:
            parts.append("error:\n" + "\n".join(error_lines).rstrip())

        final_result = "\n".join(parts).strip()
        return self._truncate_output(final_result if final_result else "Finished execution, but no result.")

    async def aexecute_command(
        self,
        command: str,
        cwd: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self._ensure_sandbox_initialized()
        sandbox_id = sandbox_id or self._sandbox_id
        if not sandbox_id:
            raise LimitedCodeSandboxNotInitializedException("Sandbox not initialized.")

        payload: dict[str, Any] = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        url = f"{self.base_url}/sandboxes/{sandbox_id}/processes/cmd"
        session = await self._get_session()

        try:
            async with session.post(url, headers=self._get_headers(), json=payload) as response:
                response_text = await response.text()
                if not response.ok:
                    raise LimitedCodeSandboxHTTPException(
                        f"HTTP {response.status}: {response_text}"
                    )
                return json.loads(response_text) if response_text else {}
        except asyncio.TimeoutError as exc:
            raise LimitedCodeSandboxTimeoutException("Command execution timed out.") from exc
        except LimitedCodeSandboxException:
            raise
        except Exception as exc:
            raise LimitedCodeSandboxExecutionException(
                f"Error during command execution: {exc}"
            ) from exc

    async def ainstall_package(
        self,
        package_name: str,
        sandbox_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> dict[str, Any]:
        command = f"sudo pip install {package_name}"
        logger.info(f"Installing package in limited code sandbox: {package_name}")
        return await self.aexecute_command(command, cwd=cwd, sandbox_id=sandbox_id)
