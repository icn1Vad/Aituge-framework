from __future__ import annotations

import base64
import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from qianxuesen_mentor.config import Settings


@dataclass(slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class _BingResultsParser(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.results: list[WebSearchResult] = []
        self._inside_result = False
        self._inside_title = False
        self._inside_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "li" and "b_algo" in classes:
            self._inside_result = True
            self._title_parts = []
            self._snippet_parts = []
            self._href = ""
            return
        if not self._inside_result:
            return
        if tag == "h2":
            self._inside_title = True
        elif tag == "a" and self._inside_title and not self._href:
            self._href = attributes.get("href", "")
        elif tag == "div" and "b_caption" in classes:
            self._inside_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._inside_result:
            return
        if tag == "h2":
            self._inside_title = False
        elif tag == "div" and self._inside_snippet:
            self._inside_snippet = False
        elif tag == "li":
            title = _compact_text("".join(self._title_parts))
            snippet = _compact_text("".join(self._snippet_parts))
            url = _decode_bing_url(self._href)
            if title and url and len(self.results) < self.limit:
                self.results.append(WebSearchResult(title=title, url=url, snippet=snippet))
            self._inside_result = False
            self._inside_title = False
            self._inside_snippet = False

    def handle_data(self, data: str) -> None:
        if self._inside_result and self._inside_title:
            self._title_parts.append(data)
        elif self._inside_result and self._inside_snippet:
            self._snippet_parts.append(data)


class WebSearchTool:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.web_search_enabled
        self.endpoint = settings.web_search_endpoint
        self.result_count = settings.web_search_result_count
        self.timeout_seconds = settings.web_search_timeout_seconds

    async def search(self, query: str) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            trust_env=True,
        ) as client:
            response = await client.get(
                self.endpoint,
                params={"q": query[:400], "count": self.result_count, "setlang": "zh-Hans"},
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "Chrome/124.0 Safari/537.36"
                    ),
                },
            )
            response.raise_for_status()
        parser = _BingResultsParser(self.result_count)
        parser.feed(response.text)
        return [item.to_dict() for item in parser.results]


class CodeInterpreterTool:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.code_interpreter_enabled
        self.timeout_seconds = settings.code_execution_timeout_seconds
        self.max_output_chars = settings.code_execution_max_output_chars

    @property
    def available(self) -> bool:
        return self.enabled

    async def execute(self, code: str) -> dict[str, Any]:
        if not self.enabled:
            return {"exit_code": None, "stdout": "", "stderr": "", "error": "代码执行器未启用"}
        with tempfile.TemporaryDirectory(prefix="qxs-code-") as temp_dir:
            script_path = os.path.join(temp_dir, "main.py")
            with open(script_path, "w", encoding="utf-8") as script:
                script.write(code)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                script_path,
                cwd=temp_dir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout_seconds,
                )
                error = None
                exit_code: int | None = process.returncode
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                stdout_bytes = b""
                stderr_bytes = b""
                error = f"执行超过 {self.timeout_seconds} 秒，已终止"
                exit_code = None
        return {
            "exit_code": exit_code,
            "stdout": _truncate_output(stdout_bytes, self.max_output_chars),
            "stderr": _truncate_output(stderr_bytes, self.max_output_chars),
            "error": error,
        }


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _truncate_output(value: bytes, limit: int) -> str:
    text = value.decode("utf-8", errors="replace").rstrip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[输出已截断]"


def _decode_bing_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if encoded.startswith("a1"):
        token = encoded[2:]
        token += "=" * (-len(token) % 4)
        try:
            decoded = base64.urlsafe_b64decode(token).decode("utf-8")
            if decoded.startswith(("http://", "https://")):
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    return value if value.startswith(("http://", "https://")) else ""
