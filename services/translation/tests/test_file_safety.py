import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from translation_service.errors import TranslationError
from translation_service.processors.file_safety import (
    safe_file_name,
    validate_docx_archive,
)


def _limits():
    return SimpleNamespace(
        max_docx_entries=10,
        max_docx_uncompressed_bytes=1024 * 1024,
        max_docx_compression_ratio=100,
    )


def test_safe_file_name_removes_both_path_separator_styles() -> None:
    assert safe_file_name("..\\folder/unsafe.docx", fallback="document") == "unsafe.docx"


def test_minimal_docx_archive_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "document.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    validate_docx_archive(path, _limits())


def test_docx_path_traversal_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("../escape", "bad")
    with pytest.raises(TranslationError) as error:
        validate_docx_archive(path, _limits())
    assert error.value.code == "UNSAFE_DOCX_ARCHIVE"
