from __future__ import annotations

from pathlib import Path

import pytest

from contract.application.ports import UploadedContract
from contract.errors import ContractError
from contract.parser.storage import ContractFileStore, safe_filename


def _upload(content: bytes = b"%PDF-contract") -> UploadedContract:
    return UploadedContract(filename="合同.pdf", content_type="application/pdf", content=content)


def test_file_store_is_content_addressed_scoped_and_idempotent(tmp_path: Path) -> None:
    store = ContractFileStore(tmp_path)

    first = store.store(_upload(), tenant_id="tenant-secret", user_id="user-secret")
    repeated = store.store(_upload(), tenant_id="tenant-secret", user_id="user-secret")

    assert first.reused is False
    assert repeated.reused is True
    assert first.relative_path == repeated.relative_path
    assert first.absolute_path.read_bytes() == b"%PDF-contract"
    assert "tenant-secret" not in first.relative_path
    assert "user-secret" not in first.relative_path
    assert first.absolute_path.is_relative_to(tmp_path.resolve())

    another_tenant = store.store(_upload(), tenant_id="another-tenant", user_id="user-secret")
    assert another_tenant.relative_path != first.relative_path


def test_file_store_refuses_existing_content_address_tampering(tmp_path: Path) -> None:
    store = ContractFileStore(tmp_path)
    saved = store.store(_upload(), tenant_id="tenant", user_id="user")
    saved.absolute_path.write_bytes(b"tampered")

    with pytest.raises(ContractError) as captured:
        store.store(_upload(), tenant_id="tenant", user_id="user")

    assert captured.value.code == "INTERNAL_ERROR"


def test_file_store_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        ContractFileStore(tmp_path).resolve("../outside.pdf")


def test_safe_filename_removes_paths_controls_and_trailing_dots() -> None:
    assert safe_filename(r"C:\fake\采购<>合同.docx") == "采购_合同.docx"
    assert safe_filename("超" * 250 + ".pdf").endswith(".pdf")
    assert len(safe_filename("超" * 250 + ".pdf")) == 200
