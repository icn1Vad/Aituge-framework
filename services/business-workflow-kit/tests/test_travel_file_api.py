from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from business_workflow_kit import travel_file_api


class FakeFiles:
    async def get_file(self, *, file_id, tenant_id):
        if (file_id, tenant_id) != ("file-1", "tenant-1"):
            return None
        return SimpleNamespace(id=file_id, status="succeeded", file_name="餐饮发票.pdf",
                               file_size=100)

    async def get_text_slice(self, *, file_id, tenant_id, offset, limit):
        return {"content": "增值税普通发票\n餐饮服务\n发票号码 12345678901234567890\n"
                           "购买方 华泰人寿保险股份有限公司\n价税合计 ¥100.00",
                "has_more": False, "truncated_at_extract": False}


@pytest.mark.asyncio
async def test_invoice_recognition_consumes_platform_file_without_reupload():
    result = await travel_file_api.recognize_invoice_files(FakeFiles(), ["file-1", "file-1"], "tenant-1")
    assert result.file_count == 1
    assert result.total_amount == 100
    assert result.invoices[0].invoice_id == "file-1"
    assert result.invoices[0].recognition_method == "PLATFORM_PARSE"


@pytest.mark.asyncio
async def test_attendance_does_not_infer_a_signature_from_plain_text(monkeypatch):
    monkeypatch.delenv("INVOICE_OCR_BASE_URL", raising=False)
    with pytest.raises(HTTPException) as error:
        await travel_file_api.recognize_attendance_file(FakeFiles(), "file-1", "tenant-1")
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_cross_tenant_file_id_is_not_accepted():
    with pytest.raises(HTTPException) as error:
        await travel_file_api.recognize_invoice_files(FakeFiles(), ["file-1"], "other")
    assert error.value.status_code == 404
