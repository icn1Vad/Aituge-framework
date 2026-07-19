"""Minimal local Python execution tool.

This is intentionally not a strong security sandbox. It prioritizes making
local code execution work for trusted development/test flows, with only basic
operational limits: temporary working directory, timeout, output truncation,
and cleanup.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import textwrap
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union

from tool.artifacts import ArtifactPublisher, ArtifactRef


TRIPLE_QUOTE_PATTERN = re.compile(r"```[^\n]*\n(.+?)```", re.DOTALL)
XML_CODE_PATTERN = re.compile(r"<code>(.*?)</code>", re.DOTALL)
ARTIFACT_EXTENSIONS = {".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


@dataclass(slots=True)
class LimitedLocalPythonConfig:
    """Config for the minimal local Python runtime."""

    enabled: bool = True
    python_executable: str = sys.executable
    timeout_seconds: int = 20
    max_output_chars: int = 50_000
    work_dir: Optional[Path] = None
    artifact_publisher: ArtifactPublisher | None = None
    max_artifact_files: int = 20
    env: dict[str, str] = field(default_factory=dict)
    keep_work_dir: bool = False
    cleanup_run_dir: bool = False


@dataclass(slots=True)
class LocalPythonResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    artifacts: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class LimitedLocalPythonTool:
    """Execute Python code in a temporary local working directory."""

    def __init__(self, config: LimitedLocalPythonConfig | None = None):
        self.config = config or LimitedLocalPythonConfig()
        self._owned_temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self.work_dir = self._prepare_work_dir()

    def _prepare_work_dir(self) -> Path:
        if self.config.work_dir is not None:
            path = Path(self.config.work_dir).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            return path

        self._owned_temp_dir = tempfile.TemporaryDirectory(prefix="tuge-local-python-")
        return Path(self._owned_temp_dir.name)

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
            return textwrap.dedent(str(code)).strip()
        except Exception:
            if isinstance(params, str):
                return textwrap.dedent(params).strip()
            return ""

    def _truncate(self, text: str) -> str:
        limit = self.config.max_output_chars
        if limit <= 0 or len(text) <= limit:
            return text
        omitted = len(text) - limit
        return f"{text[:limit]}\n\n[LimitedLocalPython truncated {omitted} characters]"

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.config.env)
        return env

    def _create_run_dir(self) -> tuple[str, Path]:
        run_id = uuid.uuid4().hex
        run_dir = self.work_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_id, run_dir

    async def _collect_artifacts(self, run_dir: Path) -> tuple[list[dict[str, str]], str | None]:
        publisher = self.config.artifact_publisher
        if publisher is None:
            return [], None

        artifacts: list[dict[str, str]] = []
        for path in sorted(run_dir.iterdir()):
            if len(artifacts) >= self.config.max_artifact_files:
                break
            if not path.is_file() or path.name == "main.py":
                continue
            if path.suffix.lower() not in ARTIFACT_EXTENSIONS:
                continue
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                ref: ArtifactRef = await publisher.publish(
                    path,
                    sequence=len(artifacts) + 1,
                    mime=mime_type,
                )
            except Exception as exc:
                return artifacts, f"Artifact publishing failed: {exc}"
            artifacts.append(ref.to_dict())
        return artifacts, None

    async def aexecute(self, code: Union[str, dict]) -> str:
        if not self.config.enabled:
            return LocalPythonResult(exit_code=None, error="LimitedLocalPythonInterpreter is disabled.").to_json()

        code_text = self._extract_code(code)
        if not code_text:
            return LocalPythonResult(exit_code=None, error="No Python code was provided.").to_json()

        _, run_dir = self._create_run_dir()
        script_path = run_dir / "main.py"
        script_path.write_text(code_text, encoding="utf-8")

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.config.python_executable,
                    str(script_path),
                    cwd=str(run_dir),
                    env=self._build_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                if "proc" in locals() and proc.returncode is None:
                    proc.kill()
                    await proc.communicate()
                return LocalPythonResult(
                    exit_code=None,
                    error=f"Execution exceeded {self.config.timeout_seconds} seconds.",
                ).to_json()

            stdout = stdout_bytes.decode("utf-8", errors="replace").rstrip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").rstrip()
            artifacts, publish_error = await self._collect_artifacts(run_dir)
            return LocalPythonResult(
                exit_code=proc.returncode,
                stdout=self._truncate(stdout),
                stderr=self._truncate(stderr),
                error=publish_error,
                artifacts=artifacts,
            ).to_json()
        finally:
            if self.config.cleanup_run_dir:
                shutil.rmtree(run_dir, ignore_errors=True)

    async def acleanup(self) -> None:
        if self.config.keep_work_dir:
            return
        if self._owned_temp_dir is not None:
            self._owned_temp_dir.cleanup()
            self._owned_temp_dir = None
