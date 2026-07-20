from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx

from translation_service.config import Settings
from translation_service.domain.models import PdfOutputMode
from translation_service.errors import TranslationError


@dataclass(frozen=True, slots=True)
class BabelDocArtifact:
    output_type: str
    path: Path
    file_name: str
    mime_type: str
    size: int
    sha256: str


class BabelDocClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def translate(
        self,
        *,
        source_path: Path,
        source_code: str,
        target_code: str,
        output_mode: PdfOutputMode,
        translation_task_id: str,
        request_id: str,
        destination_dir: Path,
        glossary_path: Path | None = None,
    ) -> list[BabelDocArtifact]:
        try:
            return await self._translate_impl(
                source_path=source_path,
                source_code=source_code,
                target_code=target_code,
                output_mode=output_mode,
                translation_task_id=translation_task_id,
                request_id=request_id,
                destination_dir=destination_dir,
                glossary_path=glossary_path,
            )
        except TranslationError:
            raise
        except httpx.TimeoutException as exc:
            raise TranslationError(
                "BABELDOC_TIMEOUT",
                "BabelDOC sidecar request timed out",
                status_code=504,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise TranslationError(
                "BABELDOC_UNAVAILABLE",
                "BabelDOC sidecar is unavailable",
                status_code=502,
                retryable=True,
            ) from exc

    async def _translate_impl(
        self,
        *,
        source_path: Path,
        source_code: str,
        target_code: str,
        output_mode: PdfOutputMode,
        translation_task_id: str,
        request_id: str,
        destination_dir: Path,
        glossary_path: Path | None,
    ) -> list[BabelDocArtifact]:
        headers = {
            "X-Internal-Token": self._settings.babeldoc_internal_token.get_secret_value(),
            "X-Request-Id": request_id,
        }
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(
            base_url=self._settings.babeldoc_base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        ) as client:
            with ExitStack() as stack:
                source = stack.enter_context(source_path.open("rb"))
                files = {"file": (source_path.name, source, "application/pdf")}
                if glossary_path is not None:
                    glossary = stack.enter_context(glossary_path.open("rb"))
                    files["glossary"] = (
                        glossary_path.name,
                        glossary,
                        "text/csv",
                    )
                response = await client.post(
                    "/internal/v1/pdf-translations",
                    data={
                        "translation_task_id": translation_task_id,
                        "source_language": source_code,
                        "target_language": target_code,
                        "output_mode": output_mode.value,
                    },
                    files=files,
                )
            payload = _success_payload(response)
            engine_task_id = str(payload.get("task_id") or "")
            if not engine_task_id:
                raise TranslationError(
                    "BABELDOC_PROTOCOL_ERROR",
                    "BabelDOC sidecar did not return a task identifier",
                    status_code=502,
                    retryable=True,
                )
            await self._wait_for_completion(client, engine_task_id)
            outputs_response = await client.get(
                f"/internal/v1/pdf-translations/{engine_task_id}/outputs"
            )
            output_rows = _success_data(outputs_response)
            if not isinstance(output_rows, list):
                raise TranslationError(
                    "BABELDOC_PROTOCOL_ERROR",
                    "BabelDOC sidecar returned invalid artifact metadata",
                    status_code=502,
                    retryable=True,
                )
            destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            artifacts: list[BabelDocArtifact] = []
            for row in output_rows:
                if not isinstance(row, dict):
                    continue
                output_type = str(row.get("output_type") or "")
                if output_type not in {"MONO", "DUAL"}:
                    continue
                destination = destination_dir / f"translated-{output_type.lower()}.pdf"
                await self._download_output(
                    client,
                    engine_task_id=engine_task_id,
                    output_type=output_type,
                    destination=destination,
                    expected_sha256=str(row.get("sha256") or ""),
                    expected_size=int(row.get("size") or 0),
                )
                artifacts.append(
                    BabelDocArtifact(
                        output_type=output_type,
                        path=destination,
                        file_name=destination.name,
                        mime_type="application/pdf",
                        size=int(row["size"]),
                        sha256=str(row["sha256"]),
                    )
                )
            required = {
                PdfOutputMode.MONO: {"MONO"},
                PdfOutputMode.DUAL: {"DUAL"},
                PdfOutputMode.BOTH: {"MONO", "DUAL"},
            }[output_mode]
            if {artifact.output_type for artifact in artifacts} != required:
                raise TranslationError(
                    "BABELDOC_OUTPUT_MISSING",
                    "BabelDOC sidecar did not return all requested outputs",
                    status_code=502,
                    retryable=True,
                )
            return artifacts

    async def _wait_for_completion(
        self, client: httpx.AsyncClient, engine_task_id: str
    ) -> None:
        deadline = time.monotonic() + self._settings.babeldoc_timeout_seconds
        while True:
            if time.monotonic() >= deadline:
                raise TranslationError(
                    "BABELDOC_TIMEOUT",
                    "PDF translation exceeded the configured timeout",
                    status_code=504,
                    retryable=True,
                )
            response = await client.get(
                f"/internal/v1/pdf-translations/{engine_task_id}"
            )
            task = _success_payload(response)
            status = str(task.get("status") or "")
            if status == "SUCCEEDED":
                return
            if status in {"FAILED", "INTERRUPTED"}:
                raise TranslationError(
                    str(task.get("error_code") or "BABELDOC_FAILED"),
                    str(task.get("error_message") or "PDF translation failed"),
                    status_code=502,
                    retryable=bool(task.get("retryable", True)),
                )
            await asyncio.sleep(self._settings.babeldoc_poll_interval_seconds)

    @staticmethod
    async def _download_output(
        client: httpx.AsyncClient,
        *,
        engine_task_id: str,
        output_type: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        digest = hashlib.sha256()
        size = 0
        try:
            async with client.stream(
                "GET",
                f"/internal/v1/pdf-translations/{engine_task_id}/outputs/{output_type}",
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _raise_sidecar_error(response)
                with destination.open("xb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if size != expected_size or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise TranslationError(
                "BABELDOC_ARTIFACT_INTEGRITY_MISMATCH",
                "Downloaded PDF does not match sidecar artifact metadata",
                status_code=502,
                retryable=True,
            )
        with destination.open("rb") as source:
            if source.read(5) != b"%PDF-":
                destination.unlink(missing_ok=True)
                raise TranslationError(
                    "BABELDOC_OUTPUT_INVALID",
                    "Downloaded BabelDOC artifact is not a PDF",
                    status_code=502,
                    retryable=True,
                )


def _success_payload(response: httpx.Response) -> dict:
    data = _success_data(response)
    if not isinstance(data, dict):
        raise TranslationError(
            "BABELDOC_PROTOCOL_ERROR",
            "BabelDOC sidecar returned an invalid response",
            status_code=502,
            retryable=True,
        )
    return data


def _success_data(response: httpx.Response):
    if response.status_code >= 400:
        _raise_sidecar_error(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranslationError(
            "BABELDOC_PROTOCOL_ERROR",
            "BabelDOC sidecar returned non-JSON metadata",
            status_code=502,
            retryable=True,
        ) from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise TranslationError(
            "BABELDOC_PROTOCOL_ERROR",
            "BabelDOC sidecar returned an unsuccessful response",
            status_code=502,
            retryable=True,
        )
    return payload.get("data")


def _raise_sidecar_error(response: httpx.Response) -> None:
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
    except ValueError:
        error = None
    if not isinstance(error, dict):
        raise TranslationError(
            "BABELDOC_UNAVAILABLE",
            "BabelDOC sidecar request failed",
            status_code=502,
            retryable=response.status_code >= 500,
        )
    raise TranslationError(
        str(error.get("code") or "BABELDOC_FAILED"),
        str(error.get("message") or "BabelDOC sidecar request failed")[:500],
        status_code=502,
        retryable=bool(error.get("retryable")),
    )
