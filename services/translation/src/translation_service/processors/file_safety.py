from __future__ import annotations

import hashlib
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import UploadFile
from pypdf import PdfReader

from translation_service.config import Settings
from translation_service.domain.models import TranslationFileType
from translation_service.errors import TranslationError

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_PDF_SIGNATURE = b"%PDF-"
_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

_EXTENSIONS = {
    ".pdf": TranslationFileType.PDF,
    ".doc": TranslationFileType.DOC,
    ".docx": TranslationFileType.DOCX,
}
_MIME_TYPES = {
    TranslationFileType.PDF: {"application/pdf", "application/octet-stream"},
    TranslationFileType.DOC: {
        "application/msword",
        "application/octet-stream",
        "application/x-ole-storage",
    },
    TranslationFileType.DOCX: {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
}


@dataclass(frozen=True, slots=True)
class SavedUpload:
    staging_dir: Path
    path: Path
    original_name: str
    file_type: TranslationFileType
    media_type: str
    size: int
    sha256: str
    page_count: int | None = None

    def move_to_task(self, temp_root: Path, task_id: str) -> "SavedUpload":
        destination_dir = temp_root / task_id
        if destination_dir.exists():
            raise TranslationError(
                "TEMP_DIRECTORY_COLLISION", "Task temporary directory already exists"
            )
        self.staging_dir.rename(destination_dir)
        return SavedUpload(
            staging_dir=destination_dir,
            path=destination_dir / self.path.name,
            original_name=self.original_name,
            file_type=self.file_type,
            media_type=self.media_type,
            size=self.size,
            sha256=self.sha256,
            page_count=self.page_count,
        )


async def save_and_validate_upload(
    upload: UploadFile, settings: Settings
) -> SavedUpload:
    original_name = safe_file_name(upload.filename, fallback="document")
    extension = Path(original_name).suffix.lower()
    file_type = _EXTENSIONS.get(extension)
    if file_type is None:
        await upload.close()
        raise TranslationError(
            "UNSUPPORTED_FILE_TYPE", "Only PDF, DOC and DOCX files are supported"
        )
    declared_type = (upload.content_type or "application/octet-stream").split(
        ";", maxsplit=1
    )[0].strip().lower()
    if declared_type not in _MIME_TYPES[file_type]:
        await upload.close()
        raise TranslationError(
            "MIME_MISMATCH", "Declared MIME type does not match the file extension"
        )

    staging_dir = settings.temp_root / f"staging-{uuid.uuid4().hex}"
    staging_dir.mkdir(mode=0o700)
    path = staging_dir / f"source{extension}"
    digest = hashlib.sha256()
    total = 0
    first_bytes = bytearray()
    try:
        with path.open("xb") as destination:
            while chunk := await upload.read(1024 * 1024):
                if len(first_bytes) < 8:
                    first_bytes.extend(chunk[: 8 - len(first_bytes)])
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise TranslationError(
                        "FILE_TOO_LARGE",
                        "File exceeds the configured upload size limit",
                        status_code=413,
                    )
                digest.update(chunk)
                destination.write(chunk)
        page_count = _validate_real_type(
            path, file_type, bytes(first_bytes), settings
        )
        return SavedUpload(
            staging_dir=staging_dir,
            path=path,
            original_name=original_name,
            file_type=file_type,
            media_type=_canonical_mime(file_type),
            size=total,
            sha256=digest.hexdigest(),
            page_count=page_count,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    finally:
        await upload.close()


def safe_file_name(value: str | None, *, fallback: str) -> str:
    candidate = Path((value or "").replace("\\", "/")).name
    candidate = _CONTROL_CHARACTERS.sub("", candidate).strip().strip(".")
    if not candidate:
        candidate = fallback
    if len(candidate) <= 180:
        return candidate
    suffix = Path(candidate).suffix[:20]
    return f"{candidate[: 180 - len(suffix)]}{suffix}"


def clean_all_temp_directories(temp_root: Path) -> None:
    for child in temp_root.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def extract_pdf_sample(path: Path, max_pages: int, max_chars: int) -> str:
    try:
        reader = PdfReader(path, strict=False)
        parts: list[str] = []
        for page in reader.pages[:max_pages]:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
            if sum(map(len, parts)) >= max_chars:
                break
        return "\n".join(parts)[:max_chars]
    except Exception as exc:
        raise TranslationError(
            "PDF_TEXT_EXTRACTION_FAILED", "Could not extract text for language detection"
        ) from exc


def validate_docx_archive(path: Path, settings: Settings) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > settings.max_docx_entries:
                raise TranslationError(
                    "DOCX_ENTRY_LIMIT_EXCEEDED",
                    "DOCX contains too many ZIP entries",
                    status_code=413,
                )
            names: set[str] = set()
            total_uncompressed = 0
            for info in infos:
                normalized = PurePosixPath(info.filename.replace("\\", "/"))
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise TranslationError(
                        "UNSAFE_DOCX_ARCHIVE", "DOCX contains an unsafe ZIP path"
                    )
                if info.filename in names:
                    raise TranslationError(
                        "UNSAFE_DOCX_ARCHIVE", "DOCX contains duplicate ZIP entries"
                    )
                names.add(info.filename)
                if info.flag_bits & 0x1:
                    raise TranslationError(
                        "ENCRYPTED_DOCX_UNSUPPORTED",
                        "Encrypted DOCX files are not supported",
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > settings.max_docx_uncompressed_bytes:
                    raise TranslationError(
                        "DOCX_UNCOMPRESSED_LIMIT_EXCEEDED",
                        "DOCX uncompressed content exceeds the configured limit",
                        status_code=413,
                    )
                if info.file_size:
                    ratio = info.file_size / max(1, info.compress_size)
                    if ratio > settings.max_docx_compression_ratio:
                        raise TranslationError(
                            "DOCX_COMPRESSION_RATIO_EXCEEDED",
                            "DOCX ZIP compression ratio exceeds the configured limit",
                            status_code=413,
                        )
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names):
                raise TranslationError(
                    "INVALID_DOCX", "DOCX is missing required OpenXML parts"
                )
    except TranslationError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise TranslationError("INVALID_DOCX", "DOCX archive could not be parsed") from exc


def _validate_real_type(
    path: Path,
    file_type: TranslationFileType,
    first_bytes: bytes,
    settings: Settings,
) -> int | None:
    if file_type is TranslationFileType.PDF:
        if not first_bytes.startswith(_PDF_SIGNATURE):
            raise TranslationError("MIME_MISMATCH", "File content is not PDF")
        try:
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise TranslationError(
                    "ENCRYPTED_PDF_UNSUPPORTED", "Encrypted PDF files are not supported"
                )
            page_count = len(reader.pages)
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError("INVALID_PDF", "PDF structure could not be parsed") from exc
        if page_count <= 0:
            raise TranslationError("INVALID_PDF", "PDF does not contain any pages")
        if page_count > settings.max_pdf_pages:
            raise TranslationError(
                "PDF_PAGE_LIMIT_EXCEEDED",
                f"PDF contains {page_count} pages; maximum is {settings.max_pdf_pages}",
                status_code=413,
            )
        return page_count
    if file_type is TranslationFileType.DOC:
        if not first_bytes.startswith(_OLE_SIGNATURE):
            raise TranslationError("MIME_MISMATCH", "File content is not a DOC file")
        return None
    if not first_bytes.startswith(_ZIP_SIGNATURES):
        raise TranslationError("MIME_MISMATCH", "File content is not DOCX")
    validate_docx_archive(path, settings)
    return None


def _canonical_mime(file_type: TranslationFileType) -> str:
    return {
        TranslationFileType.PDF: "application/pdf",
        TranslationFileType.DOC: "application/msword",
        TranslationFileType.DOCX: (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    }[file_type]
