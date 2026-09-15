import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional
from fastapi import UploadFile
from backend.attachments.vendor.file.store.file_store_helper import file_store
from pydantic import BaseModel
from loguru import logger
import json


@dataclass
class StoredFileInfo:
    """Result of uploading an UploadFile to the file_store.

    Decoupled from KbFileEntity/FileItem so the new /v1/files resource can reuse it
    without dragging in knowledgebase semantics (kb_id).
    """
    file_name: str
    file_path: str        # stored path inside file_store
    file_extension: str   # lowercased, includes leading dot
    file_md5: str
    file_size: int


@dataclass
class UploadPreview:
    """md5/size/extension peeked from an UploadFile without hitting storage.
    Useful for dedup lookups before paying the OSS write cost.
    """
    file_name: str
    file_extension: str
    file_md5: str
    file_size: int


def preview_upload(upload: UploadFile) -> UploadPreview:
    file_name = upload.filename
    file_data = upload.file
    file_data.seek(0)
    raw = file_data.read()
    file_md5 = hashlib.md5(raw).hexdigest()
    file_size = len(raw)
    file_data.seek(0)
    extension = os.path.splitext(file_name)[1].lower()
    return UploadPreview(
        file_name=file_name,
        file_extension=extension,
        file_md5=file_md5,
        file_size=file_size,
    )


async def write_upload_to_store(
    upload: UploadFile,
    destination_path: str,
    tenant_id: str,
) -> StoredFileInfo:
    """Low-level helper: persist an UploadFile to the file_store and return
    only the facts we care about (md5/size/ext). Used by both the legacy
    `upload_form_files_async` path and the new `FileResourceService`.
    """
    preview = preview_upload(upload)
    upload_result = await file_store.write_async(
        file=upload.file,
        file_name=preview.file_name,
        file_path=destination_path,
        tenant_id=tenant_id,
    )
    return StoredFileInfo(
        file_name=preview.file_name,
        file_path=upload_result.file_path,
        file_extension=preview.file_extension,
        file_md5=preview.file_md5,
        file_size=preview.file_size,
    )
