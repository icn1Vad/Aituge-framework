from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from translation_service.config import Settings
from translation_service.errors import TranslationError

logger = logging.getLogger(__name__)


class LibreOfficeConverter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def doc_to_docx(self, source: Path, output_dir: Path) -> Path:
        return await self._convert(
            source=source,
            output_dir=output_dir,
            conversion_filter="docx:Office Open XML Text",
            expected_suffix=".docx",
        )

    async def docx_to_doc(self, source: Path, output_dir: Path) -> Path:
        return await self._convert(
            source=source,
            output_dir=output_dir,
            conversion_filter="doc:MS Word 97",
            expected_suffix=".doc",
        )

    async def _convert(
        self,
        *,
        source: Path,
        output_dir: Path,
        conversion_filter: str,
        expected_suffix: str,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_dir = output_dir / "lo-profile"
        profile_dir.mkdir(mode=0o700)
        command = [
            self._settings.libreoffice_binary,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            conversion_filter,
            "--outdir",
            str(output_dir),
            str(source),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=self._settings.libreoffice_timeout_seconds
            )
        except asyncio.CancelledError:
            await _terminate_process_group(process)
            raise
        except TimeoutError as exc:
            await _terminate_process_group(process)
            raise TranslationError(
                "LIBREOFFICE_TIMEOUT",
                "Office conversion exceeded the configured timeout",
                status_code=504,
                retryable=True,
            ) from exc
        if process.returncode != 0:
            logger.error(
                "LibreOffice conversion failed for %s with exit code %s",
                source.name,
                process.returncode,
            )
            raise TranslationError(
                "LIBREOFFICE_CONVERSION_FAILED",
                "Office document conversion failed",
                status_code=422,
            )
        candidates = [
            path
            for path in output_dir.iterdir()
            if path.is_file() and path.suffix.lower() == expected_suffix
        ]
        if len(candidates) != 1:
            logger.error(
                "LibreOffice produced %s candidate files for %s; output=%s",
                len(candidates),
                source.name,
                output.decode("utf-8", errors="replace")[-500:],
            )
            raise TranslationError(
                "LIBREOFFICE_OUTPUT_MISSING",
                "Office conversion did not produce the expected output",
            )
        return candidates[0]


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=10)
    except (ProcessLookupError, TimeoutError):
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
