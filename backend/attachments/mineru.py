"""Async HTTP adapter for an independently deployed MinerU service.

No model weights, Torch, web UI, or Agent runtime are imported here. The caller
owns file storage, task scheduling, and persistence of the returned Markdown.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from .extraction_models import DocumentExtractionError, ExtractedDocument

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp"})


@dataclass(frozen=True, slots=True)
class MinerUConfig:
    endpoint: str
    backend: str = "vlm-engine"
    timeout_seconds: float = 600.0
    token: str = ""

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise ValueError("MinerU endpoint is required")
        if self.timeout_seconds <= 0:
            raise ValueError("MinerU timeout must be positive")

    @classmethod
    def from_env(cls) -> MinerUConfig:
        return cls(
            endpoint=os.getenv("AITUGE_MINERU_ENDPOINT", "").strip(),
            backend=os.getenv("AITUGE_MINERU_BACKEND", "vlm-engine"),
            timeout_seconds=float(os.getenv("AITUGE_MINERU_TIMEOUT_SECONDS", "600")),
            token=os.getenv("AITUGE_MINERU_TOKEN", "").strip(),
        )


class MinerUExtractor:
    def __init__(self, config: MinerUConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client

    @staticmethod
    def supports(file_name: str) -> bool:
        return Path(file_name).suffix.lower() in SUPPORTED_EXTENSIONS

    async def extract(self, content: bytes, file_name: str) -> ExtractedDocument:
        if not self.supports(file_name):
            raise DocumentExtractionError(f"MinerU 不支持此附件格式：{file_name}")
        if not content:
            raise DocumentExtractionError("附件为空")
        try:
            if self._client is not None:
                response = await self._request(self._client, content, file_name)
            else:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await self._request(client, content, file_name)
        except httpx.RequestError as exc:
            raise DocumentExtractionError(f"MinerU 服务连接或读取失败：{type(exc).__name__}") from exc

        if not response.is_success:
            detail = response.reason_phrase
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = payload.get("error") or payload.get("detail") or payload.get("message") or detail
            except ValueError:
                pass
            raise DocumentExtractionError(f"MinerU 识别失败（HTTP {response.status_code}）：{detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DocumentExtractionError("MinerU 返回了无效的 JSON") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, dict) or not results:
            raise DocumentExtractionError("MinerU 未返回文档识别结果")
        texts = []
        for result in results.values():
            text = result.get("md_content") if isinstance(result, dict) else None
            if not isinstance(text, str):
                raise DocumentExtractionError("MinerU 返回的文档结果缺少 md_content")
            texts.append(text)
        markdown = "\n\n".join(texts).strip()
        if not markdown:
            raise DocumentExtractionError("MinerU 未识别到可读取的文字")
        return ExtractedDocument(
            file_name=file_name, markdown=markdown, provider="mineru", backend=self.config.backend,
        )

    async def _request(self, client: httpx.AsyncClient, content: bytes, file_name: str) -> httpx.Response:
        return await client.post(
            self.config.endpoint.rstrip("/") + "/file_parse",
            headers={"Authorization": f"Bearer {self.config.token}"} if self.config.token else {},
            timeout=self.config.timeout_seconds,
            files={"files": (file_name, content, mimetypes.guess_type(file_name)[0] or "application/octet-stream")},
            data={
                "backend": self.config.backend,
                "parse_method": "auto",
                "return_md": "true",
                "return_images": "false",
                "response_format_zip": "false",
            },
        )
