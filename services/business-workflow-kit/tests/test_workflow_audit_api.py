from types import SimpleNamespace

import pytest

from business_workflow_kit import workflow_audit_api as audit_api


class FakeFiles:
    async def get_file(self, *, file_id, tenant_id):
        return SimpleNamespace(status="succeeded") if (file_id, tenant_id) == ("file-1", "7") else None

    async def get_text_slice(self, *, file_id, tenant_id, offset, limit):
        return {"content": "合同编号 HT-1，第二期金额 100.00 元。", "total_length": 26,
                "has_more": False, "truncated_at_extract": False}


@pytest.mark.asyncio
async def test_extract_uses_real_catalog_ids(monkeypatch):
    async def complete(self, *, model_id, prompt, pages=(), catalog=()):
        assert "证据编号 F" in prompt
        source_id = prompt.split("证据编号 ", 1)[1].split(":", 1)[0]
        return ({"classification": {"kind": "CONTRACT", "evidence_refs": [source_id]},
                 "facts": [{"field": "contract_number", "value": "HT-1", "evidence_refs": [source_id]}],
                 "installments": []}, {"model": "test"})

    monkeypatch.setattr(audit_api.ModelJsonClient, "complete", complete)
    result = await audit_api.extract_parsed_file(
        audit_api.ExtractFileRequest(file_id="file-1", tenant_id="7", kind="AUTO"), FakeFiles()
    )
    assert result["status"] == "EXTRACTED"
    assert result["kind"] == "CONTRACT"
    assert result["facts"][0]["evidence_refs"] == [result["evidence"][0]["source_id"]]


@pytest.mark.asyncio
async def test_missing_file_is_not_interpreted():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await audit_api.extract_parsed_file(
            audit_api.ExtractFileRequest(file_id="file-1", tenant_id="other", kind="AUTO"), FakeFiles()
        )
    assert error.value.status_code == 404
