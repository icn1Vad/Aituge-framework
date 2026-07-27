from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from contract.application.idempotency import sha256_bytes
from contract.application.ports import UploadedContract
from contract.errors import ContractError


_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._()（） -]+")


@dataclass(frozen=True, slots=True)
class StoredContractFile:
    original_name: str
    content_type: str
    file_type: str
    file_size: int
    content_hash: str
    relative_path: str
    absolute_path: Path
    reused: bool


class ContractFileStore:
    """Content-addressed technical copies scoped without exposing tenant or user IDs in paths."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        upload: UploadedContract,
        *,
        tenant_id: str,
        user_id: str,
    ) -> StoredContractFile:
        original_name = safe_filename(upload.filename)
        suffix = Path(original_name).suffix.lower()
        file_type = suffix.lstrip(".")
        content_hash = sha256_bytes(upload.content)
        digest = content_hash.removeprefix("sha256:")
        scope = hashlib.sha256(f"{tenant_id}\0{user_id}".encode("utf-8")).hexdigest()[:24]
        relative_path = Path("files") / scope / digest[:2] / f"{digest}{suffix}"
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            self._verify_existing(target, upload.content, content_hash)
            reused = True
        else:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(upload.content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                target.chmod(0o600)
                self._verify_existing(target, upload.content, content_hash)
            finally:
                temporary.unlink(missing_ok=True)
            reused = False

        return StoredContractFile(
            original_name=original_name,
            content_type=upload.content_type,
            file_type=file_type,
            file_size=len(upload.content),
            content_hash=content_hash,
            relative_path=relative_path.as_posix(),
            absolute_path=target,
            reused=reused,
        )

    def resolve(self, relative_path: str | Path) -> Path:
        candidate = (self.data_root / Path(relative_path)).resolve()
        if not candidate.is_relative_to(self.data_root):
            raise ContractError(
                "INTERNAL_ERROR",
                "合同技术文件路径无效",
                status_code=500,
            )
        return candidate

    @staticmethod
    def _verify_existing(target: Path, content: bytes, expected_hash: str) -> None:
        if target.is_symlink():
            raise ContractError(
                "INTERNAL_ERROR",
                "合同技术文件路径不能是符号链接",
                status_code=500,
            )
        existing = target.read_bytes()
        if len(existing) != len(content) or sha256_bytes(existing) != expected_hash:
            raise ContractError(
                "INTERNAL_ERROR",
                "合同技术文件完整性校验失败",
                status_code=500,
            )


def safe_filename(filename: str) -> str:
    base = Path((filename or "contract").replace("\\", "/")).name
    value = _SAFE_FILENAME.sub("_", base).strip(" .")
    if not value:
        return "contract"
    if len(value) <= 200:
        return value
    suffix = Path(value).suffix[:20]
    stem_limit = 200 - len(suffix)
    return value[:stem_limit].rstrip(" .") + suffix
