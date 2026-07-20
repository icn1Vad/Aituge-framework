from __future__ import annotations

import asyncio
import csv
import hashlib
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from translation_service.config import Settings
from translation_service.context import InternalRequestContext
from translation_service.domain.models import (
    ArtifactData,
    GlossaryEntry,
    OfficeOutputFormat,
    PdfOutputMode,
    TextTranslationData,
    TextTranslationRequest,
    TranslationFileType,
    TranslationLanguage,
    TranslationStage,
    TranslationTaskData,
)
from translation_service.errors import TranslationError
from translation_service.infrastructure.babeldoc import BabelDocClient
from translation_service.language.catalog import babeldoc_code, ensure_language_pair
from translation_service.language.detector import LanguageDetector
from translation_service.model.translator import BaseModelTranslator
from translation_service.processors.docx import DocxTranslator
from translation_service.processors.file_safety import (
    SavedUpload,
    extract_pdf_sample,
    safe_file_name,
    save_and_validate_upload,
    sha256_and_size,
    validate_docx_archive,
)
from translation_service.processors.libreoffice import LibreOfficeConverter
from translation_service.task.tracker import TranslationTaskTracker


@dataclass(frozen=True, slots=True)
class FileTranslationOptions:
    source_language: TranslationLanguage
    target_language: TranslationLanguage
    pdf_output_mode: PdfOutputMode
    office_output_format: OfficeOutputFormat
    glossary: list[GlossaryEntry]


@dataclass(frozen=True, slots=True)
class PendingArtifact:
    output_type: str
    path: Path
    file_name: str
    mime_type: str
    size: int
    sha256: str


class CapacityLimiter:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._active = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def try_slot(self) -> AsyncIterator[bool]:
        async with self._lock:
            acquired = self._active < self._capacity
            if acquired:
                self._active += 1
        try:
            yield acquired
        finally:
            if acquired:
                async with self._lock:
                    self._active -= 1


class TranslationApplicationService:
    def __init__(
        self,
        *,
        settings: Settings,
        tracker: TranslationTaskTracker,
        model_translator: BaseModelTranslator,
        language_detector: LanguageDetector,
        docx_translator: DocxTranslator,
        office_converter: LibreOfficeConverter,
        babeldoc_client: BabelDocClient,
    ) -> None:
        self._settings = settings
        self._tracker = tracker
        self._model_translator = model_translator
        self._language_detector = language_detector
        self._docx_translator = docx_translator
        self._office_converter = office_converter
        self._babeldoc_client = babeldoc_client
        self._text_limiter = CapacityLimiter(settings.max_concurrent_text_tasks)
        self._file_semaphore = asyncio.Semaphore(settings.max_concurrent_file_tasks)
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def translate_text(
        self,
        context: InternalRequestContext,
        request: TextTranslationRequest,
    ) -> TextTranslationData:
        ensure_language_pair(request.source_language, request.target_language)
        if len(request.text) > self._settings.max_text_chars:
            raise TranslationError(
                "TEXT_TOO_LARGE",
                "Text exceeds the configured character limit",
                status_code=413,
            )
        source_sha256 = _sha256_text(request.text)
        async with self._text_limiter.try_slot() as acquired:
            if not acquired:
                raise TranslationError(
                    "TRANSLATION_BUSY",
                    "Text translation concurrency limit has been reached",
                    status_code=429,
                    retryable=True,
                )
            task, created = await self._tracker.create_text_task(
                context=context,
                source_language=request.source_language,
                target_language=request.target_language,
                source_sha256=source_sha256,
                source_character_count=len(request.text),
            )
            if not created:
                raise TranslationError(
                    "IDEMPOTENT_TEXT_RESULT_NOT_RETAINED",
                    "This text request was already processed; full text results are not retained",
                    status_code=409,
                )
            task = await self._tracker.begin(task)
            try:
                async with self._tracker.stage(
                    task, TranslationStage.DETECTING_LANGUAGE, progress=10
                ):
                    detected = await self._language_detector.resolve(
                        requested=request.source_language,
                        target=request.target_language,
                        sample=request.text,
                        tenant_id=context.tenant_id,
                    )
                await self._tracker.update_progress(
                    task.id,
                    stage=TranslationStage.TRANSLATING,
                    progress=30,
                    metadata={
                        "detected_source_language": detected.language.value,
                        "detection_confidence": detected.confidence,
                    },
                )
                async with self._tracker.stage(
                    task, TranslationStage.TRANSLATING, progress=30
                ):
                    translated = await self._model_translator.translate_text(
                        text=request.text,
                        source=detected.language,
                        target=request.target_language,
                        tenant_id=context.tenant_id,
                        glossary=request.glossary,
                    )
                result_sha256 = _sha256_text(translated)
                await self._tracker.complete(
                    task,
                    result_metadata={
                        "detected_source_language": detected.language.value,
                        "detection_confidence": detected.confidence,
                        "source_sha256": source_sha256,
                        "result_sha256": result_sha256,
                        "source_character_count": len(request.text),
                        "result_character_count": len(translated),
                    },
                )
                return TextTranslationData(
                    task_id=task.id,
                    run_id=task.current_run_id,
                    status="SUCCEEDED",
                    source_language=request.source_language,
                    detected_source_language=detected.language,
                    target_language=request.target_language,
                    translated_text=translated,
                    source_sha256=source_sha256,
                    result_sha256=result_sha256,
                    source_character_count=len(request.text),
                    result_character_count=len(translated),
                    content_retained=False,
                )
            except asyncio.CancelledError:
                await self._tracker.interrupt(task.id)
                raise
            except Exception as exc:
                await self._tracker.fail(task.id, exc)
                raise

    async def submit_file(
        self,
        *,
        context: InternalRequestContext,
        upload,
        options: FileTranslationOptions,
    ) -> TranslationTaskData:
        ensure_language_pair(options.source_language, options.target_language)
        if (
            await self._tracker.active_file_task_count()
            >= self._settings.max_pending_file_tasks
        ):
            await upload.close()
            raise TranslationError(
                "TRANSLATION_BUSY",
                "File translation queue is full",
                status_code=429,
                retryable=True,
            )
        saved = await save_and_validate_upload(upload, self._settings)
        try:
            task, created = await self._tracker.create_file_task(
                context=context,
                source_language=options.source_language,
                target_language=options.target_language,
                source_sha256=saved.sha256,
                source_size=saved.size,
                source_file_name=saved.original_name,
                source_file_type=saved.file_type.value,
                output_format=_requested_output_format(saved.file_type, options),
            )
        except Exception:
            shutil.rmtree(saved.staging_dir, ignore_errors=True)
            raise
        if not created:
            shutil.rmtree(saved.staging_dir, ignore_errors=True)
            return await self._tracker.to_data(task)
        try:
            saved = saved.move_to_task(self._settings.temp_root, task.id)
        except Exception as exc:
            await self._tracker.fail(task.id, exc)
            shutil.rmtree(saved.staging_dir, ignore_errors=True)
            raise
        background = asyncio.create_task(
            self._run_file_task(
                task_id=task.id,
                context=context,
                saved=saved,
                options=options,
            ),
            name=f"translation-file-{task.id}",
        )
        self._background_tasks.add(background)
        background.add_done_callback(self._background_tasks.discard)
        return await self._tracker.to_data(task)

    async def get_task(
        self, task_id: str, context: InternalRequestContext
    ) -> TranslationTaskData:
        return await self._tracker.to_data(
            await self._tracker.get_owned_task(task_id, context)
        )

    async def get_artifacts(
        self, task_id: str, context: InternalRequestContext
    ) -> list[ArtifactData]:
        return await self._tracker.list_artifacts(
            await self._tracker.get_owned_task(task_id, context)
        )

    async def shutdown(self) -> None:
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_file_task(
        self,
        *,
        task_id: str,
        context: InternalRequestContext,
        saved: SavedUpload,
        options: FileTranslationOptions,
    ) -> None:
        task = None
        try:
            task = await self._tracker.get_owned_task(task_id, context)
            async with self._file_semaphore:
                task = await self._tracker.begin(task)
                await asyncio.wait_for(
                    self._process_file(
                        task=task,
                        context=context,
                        saved=saved,
                        options=options,
                    ),
                    timeout=self._settings.file_task_timeout_seconds,
                )
        except asyncio.CancelledError:
            if task is not None:
                await self._tracker.interrupt(task_id)
            raise
        except TimeoutError:
            await self._tracker.fail(
                task_id,
                TranslationError(
                    "FILE_TASK_TIMEOUT",
                    "File translation exceeded the configured total timeout",
                    status_code=504,
                    retryable=True,
                ),
            )
        except Exception as exc:
            if task is not None:
                await self._tracker.fail(task_id, exc)
        finally:
            shutil.rmtree(saved.staging_dir, ignore_errors=True)

    async def _process_file(
        self,
        *,
        task,
        context: InternalRequestContext,
        saved: SavedUpload,
        options: FileTranslationOptions,
    ) -> None:
        if saved.file_type is TranslationFileType.PDF:
            artifacts, detected, source_chars, result_chars = await self._process_pdf(
                task=task,
                context=context,
                saved=saved,
                options=options,
            )
        else:
            artifacts, detected, source_chars, result_chars = await self._process_office(
                task=task,
                context=context,
                saved=saved,
                options=options,
            )

        published: list[ArtifactData] = []
        async with self._tracker.stage(
            task, TranslationStage.PUBLISHING, progress=85
        ) as stage_run_id:
            for sequence, artifact in enumerate(artifacts, start=1):
                published.append(
                    await self._tracker.publish_artifact(
                        task=task,
                        stage_run_id=stage_run_id,
                        source_path=artifact.path,
                        sequence=sequence,
                        output_type=artifact.output_type,
                        download_name=artifact.file_name,
                        mime_type=artifact.mime_type,
                        size=artifact.size,
                        sha256=artifact.sha256,
                    )
                )
        result_sha256 = _combined_artifact_hash(published)
        await self._tracker.complete(
            task,
            result_metadata={
                "detected_source_language": detected.value,
                "source_sha256": saved.sha256,
                "result_sha256": result_sha256,
                "source_character_count": source_chars,
                "result_character_count": result_chars,
                "artifacts": [artifact.model_dump() for artifact in published],
            },
        )

    async def _process_pdf(
        self,
        *,
        task,
        context: InternalRequestContext,
        saved: SavedUpload,
        options: FileTranslationOptions,
    ) -> tuple[list[PendingArtifact], TranslationLanguage, None, None]:
        async with self._tracker.stage(
            task, TranslationStage.DETECTING_LANGUAGE, progress=10
        ):
            sample = (
                extract_pdf_sample(
                    saved.path,
                    self._settings.pdf_detection_pages,
                    self._settings.detection_sample_chars,
                )
                if options.source_language is TranslationLanguage.AUTO
                else "explicit"
            )
            detected = await self._language_detector.resolve(
                requested=options.source_language,
                target=options.target_language,
                sample=sample,
                tenant_id=context.tenant_id,
            )
        destination_dir = saved.staging_dir / "pdf-output"
        glossary_path = _write_babeldoc_glossary(
            saved.staging_dir,
            options.glossary,
            target_code=babeldoc_code(options.target_language),
        )
        async with self._tracker.stage(
            task, TranslationStage.PDF_TRANSLATING, progress=30
        ):
            outputs = await self._babeldoc_client.translate(
                source_path=saved.path,
                source_code=babeldoc_code(detected.language),
                target_code=babeldoc_code(options.target_language),
                output_mode=options.pdf_output_mode,
                translation_task_id=task.id,
                request_id=context.request_id,
                destination_dir=destination_dir,
                glossary_path=glossary_path,
            )
        artifacts = [
            PendingArtifact(
                output_type=output.output_type,
                path=output.path,
                file_name=_pdf_output_name(
                    saved.original_name, options.target_language, output.output_type
                ),
                mime_type=output.mime_type,
                size=output.size,
                sha256=output.sha256,
            )
            for output in outputs
        ]
        return artifacts, detected.language, None, None

    async def _process_office(
        self,
        *,
        task,
        context: InternalRequestContext,
        saved: SavedUpload,
        options: FileTranslationOptions,
    ) -> tuple[list[PendingArtifact], TranslationLanguage, int, int]:
        working_docx = saved.path
        if saved.file_type is TranslationFileType.DOC:
            async with self._tracker.stage(
                task, TranslationStage.CONVERTING_SOURCE, progress=10
            ):
                working_docx = await self._office_converter.doc_to_docx(
                    saved.path, saved.staging_dir / "docx-source"
                )
                validate_docx_archive(working_docx, self._settings)

        async with self._tracker.stage(
            task, TranslationStage.EXTRACTING, progress=20
        ):
            sample = self._docx_translator.extract_plain_text(
                working_docx, max_chars=self._settings.detection_sample_chars
            )
        async with self._tracker.stage(
            task, TranslationStage.DETECTING_LANGUAGE, progress=30
        ):
            detected = await self._language_detector.resolve(
                requested=options.source_language,
                target=options.target_language,
                sample=sample,
                tenant_id=context.tenant_id,
            )
        translated_docx = saved.staging_dir / "translated.docx"

        async def notice(event_type: str, subject: str, reason: str) -> None:
            await self._tracker.notice(task, event_type, subject, reason)

        async with self._tracker.stage(
            task, TranslationStage.TRANSLATING, progress=40
        ):
            result = await self._docx_translator.translate(
                source_path=working_docx,
                output_path=translated_docx,
                source_language=detected.language,
                target_language=options.target_language,
                tenant_id=context.tenant_id,
                glossary=options.glossary,
                on_notice=notice,
            )
        async with self._tracker.stage(
            task, TranslationStage.REBUILDING, progress=70
        ):
            validate_docx_archive(result.output_path, self._settings)

        output_format = _resolved_office_output_format(saved.file_type, options)
        if output_format is OfficeOutputFormat.DOC:
            async with self._tracker.stage(
                task, TranslationStage.CONVERTING_OUTPUT, progress=78
            ):
                output_path = await self._office_converter.docx_to_doc(
                    result.output_path, saved.staging_dir / "doc-output"
                )
                _validate_doc_output(output_path)
            mime_type = "application/msword"
            suffix = ".doc"
        else:
            output_path = result.output_path
            mime_type = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            suffix = ".docx"
        sha256, size = sha256_and_size(output_path)
        artifact = PendingArtifact(
            output_type=output_format.value,
            path=output_path,
            file_name=_office_output_name(
                saved.original_name, options.target_language, suffix
            ),
            mime_type=mime_type,
            size=size,
            sha256=sha256,
        )
        return (
            [artifact],
            detected.language,
            result.source_character_count,
            result.result_character_count,
        )


def _requested_output_format(
    source_type: TranslationFileType, options: FileTranslationOptions
) -> str:
    if source_type is TranslationFileType.PDF:
        return options.pdf_output_mode.value
    return _resolved_office_output_format(source_type, options).value


def _resolved_office_output_format(
    source_type: TranslationFileType, options: FileTranslationOptions
) -> OfficeOutputFormat:
    if options.office_output_format is not OfficeOutputFormat.SOURCE:
        return options.office_output_format
    return (
        OfficeOutputFormat.DOC
        if source_type is TranslationFileType.DOC
        else OfficeOutputFormat.DOCX
    )


def _office_output_name(
    source_name: str, target: TranslationLanguage, suffix: str
) -> str:
    stem = Path(safe_file_name(source_name, fallback="document")).stem[:120]
    return f"{stem}_{target.value.lower()}_translated{suffix}"


def _pdf_output_name(
    source_name: str, target: TranslationLanguage, output_type: str
) -> str:
    stem = Path(safe_file_name(source_name, fallback="document.pdf")).stem[:110]
    return f"{stem}_{target.value.lower()}_{output_type.lower()}.pdf"


def _validate_doc_output(path: Path) -> None:
    with path.open("rb") as source:
        signature = source.read(8)
    if signature != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise TranslationError(
            "LIBREOFFICE_OUTPUT_INVALID", "Converted DOC output has an invalid signature"
        )


def _write_babeldoc_glossary(
    task_dir: Path,
    glossary: list[GlossaryEntry],
    *,
    target_code: str,
) -> Path | None:
    if not glossary:
        return None
    path = task_dir / "babeldoc-glossary.csv"
    with path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["source", "target", "tgt_lng"])
        writer.writeheader()
        for entry in glossary:
            writer.writerow(
                {
                    "source": entry.source,
                    "target": entry.target,
                    "tgt_lng": target_code,
                }
            )
    path.chmod(0o600)
    return path


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _combined_artifact_hash(artifacts: list[ArtifactData]) -> str:
    if len(artifacts) == 1:
        return artifacts[0].sha256
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda item: item.output_type):
        digest.update(artifact.sha256.encode("ascii"))
    return digest.hexdigest()
