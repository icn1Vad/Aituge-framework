from __future__ import annotations

from pathlib import Path

import pytest

from proof.application.service import ProofService
from proof.config import Settings
from proof.errors import ProofError


class FileRepository:
    def __init__(self, storage_path: str, file_type: str = "pdf") -> None:
        self.file = {
            "id": "document-1",
            "name": "policy.pdf" if file_type == "pdf" else "policy.txt",
            "file_type": file_type,
            "storage_path": storage_path,
            "chunk_count": 12,
        }

    def list_files(self):
        return [{key: value for key, value in self.file.items() if key != "storage_path"}]

    def get_file(self, file_id: str):
        return dict(self.file) if file_id == self.file["id"] else None

    def list_file_chunks(self, file_id: str, *, limit: int, offset: int):
        assert file_id == self.file["id"]
        remaining = max(0, self.file["chunk_count"] - offset)
        return [
            {
                "id": f"unit-{offset + index + 1}",
                "clause_ordinal": offset + index + 1,
                "content": f"Chunk {offset + index + 1}",
            }
            for index in range(min(limit, remaining))
        ]


def _service(tmp_path: Path, repository: FileRepository) -> ProofService:
    return ProofService(
        Settings(database_url="postgresql://unused", storage_root=tmp_path),
        repository=repository,
    )


def test_file_content_resolves_pdf_inside_storage_root(tmp_path: Path) -> None:
    relative_path = "files/policy.pdf"
    pdf_path = tmp_path / relative_path
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4\n")
    service = _service(tmp_path, FileRepository(relative_path))

    assert service.list_files()["total"] == 1
    resolved = service.get_file_content("document-1")
    assert resolved == {
        "path": pdf_path.resolve(),
        "name": "policy.pdf",
        "media_type": "application/pdf",
    }


def test_file_content_rejects_non_pdf_and_missing_files(tmp_path: Path) -> None:
    non_pdf = _service(tmp_path, FileRepository("files/policy.txt", file_type="txt"))
    with pytest.raises(ProofError) as unsupported:
        non_pdf.get_file_content("document-1")
    assert unsupported.value.code == "file_content_unsupported"
    assert unsupported.value.status_code == 415

    missing = _service(tmp_path, FileRepository("files/policy.pdf"))
    with pytest.raises(ProofError) as missing_content:
        missing.get_file_content("document-1")
    assert missing_content.value.code == "file_content_not_found"

    with pytest.raises(ProofError) as missing_record:
        missing.get_file_content("missing")
    assert missing_record.value.code == "file_not_found"


def test_file_chunks_are_limited_to_ten_and_report_continuation(tmp_path: Path) -> None:
    service = _service(tmp_path, FileRepository("files/policy.pdf"))

    first = service.list_file_chunks("document-1")
    assert len(first["items"]) == 10
    assert first["has_more"] is True

    second = service.list_file_chunks("document-1", offset=10)
    assert [item["id"] for item in second["items"]] == ["unit-11", "unit-12"]
    assert second["has_more"] is False

    with pytest.raises(ProofError) as too_many:
        service.list_file_chunks("document-1", limit=11)
    assert too_many.value.code == "invalid_chunk_limit"
