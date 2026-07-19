from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from contract.config import Settings
from contract.errors import ContractError
from contract.persistence.models import DocumentBlockCreate, DocumentCreate, ReviewCreate
from contract.persistence.postgres.migrate import run_migrations
from contract.persistence.postgres.repository import ContractRepository


DATABASE_URL = os.getenv("CONTRACT_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_review_idempotency_and_parse_generation_transaction() -> None:
    settings = Settings(database_url=DATABASE_URL)
    run_migrations(settings)
    assert run_migrations(settings) == []
    repository = ContractRepository(settings)
    assert repository.health()["migration_count"] >= 1

    marker = uuid.uuid4().hex
    tenant_id = f"tenant-{marker}"
    user_id = f"user-{marker}"
    document_id = f"document-{marker}"
    review_id = f"review-{marker}"
    content_hash = "sha256:" + "1" * 64
    fingerprint = "sha256:" + "2" * 64
    document = DocumentCreate(
        document_id=document_id,
        tenant_id=tenant_id,
        user_id=user_id,
        contract_version_id=f"version-{marker}",
        original_name="contract.pdf",
        content_type="application/pdf",
        file_type="pdf",
        file_size=128,
        content_hash=content_hash,
        storage_path=f"files/{marker}.pdf",
    )
    review = ReviewCreate(
        review_id=review_id,
        tenant_id=tenant_id,
        user_id=user_id,
        business_task_id=f"task-{marker}",
        contract_version_id=document.contract_version_id,
        document_id=document_id,
        idempotency_key=f"idem-{marker}",
        request_id=f"request-{marker}",
        request_fingerprint=fingerprint,
        file_sha256=content_hash,
        perspective="PARTY_B",
        our_party_name="某某单位",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        schema_version="1.0",
    )

    try:
        created_document, reused = repository.create_document(document)
        assert reused is False
        assert created_document["id"] == document_id

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with psycopg.connect(DATABASE_URL) as conn:
                conn.execute(
                    """
                    INSERT INTO contract_parse_generation (
                      id, tenant_id, document_id, generation_no, content_hash,
                      parser_version, status
                    ) VALUES (%s, %s, %s, 1, %s, 'cross-tenant-test', 'CREATED')
                    """,
                    (
                        f"wrong-tenant-generation-{marker}",
                        "another-tenant",
                        document_id,
                        content_hash,
                    ),
                )

        reused_document, reused = repository.create_document(
            DocumentCreate(
                document_id=f"other-{document_id}",
                tenant_id=tenant_id,
                user_id=user_id,
                contract_version_id=document.contract_version_id,
                original_name="renamed.pdf",
                content_type=document.content_type,
                file_type=document.file_type,
                file_size=document.file_size,
                content_hash=document.content_hash,
                storage_path="files/other.pdf",
            )
        )
        assert reused is True
        assert reused_document["id"] == document_id

        created_review, reused = repository.create_review(review)
        assert reused is False
        assert created_review["status"] == "CREATED"

        repeated_review, reused = repository.create_review(
            ReviewCreate(
                review_id=f"other-{review_id}",
                tenant_id=tenant_id,
                user_id=user_id,
                business_task_id=review.business_task_id,
                contract_version_id=review.contract_version_id,
                document_id=document_id,
                idempotency_key=review.idempotency_key,
                request_id="request-retry",
                request_fingerprint=fingerprint,
                file_sha256=content_hash,
                perspective="PARTY_B",
                our_party_name="某某单位",
                contract_type="AUTO",
                review_attitude="NEUTRAL",
                schema_version="1.0",
            )
        )
        assert reused is True
        assert repeated_review["id"] == review_id

        with pytest.raises(ContractError) as conflict:
            repository.create_review(
                ReviewCreate(
                    review_id=f"conflict-{review_id}",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    business_task_id=review.business_task_id,
                    contract_version_id=review.contract_version_id,
                    document_id=document_id,
                    idempotency_key=review.idempotency_key,
                    request_id="request-conflict",
                    request_fingerprint="sha256:" + "3" * 64,
                    file_sha256=content_hash,
                    perspective="PARTY_A",
                    our_party_name="某某单位",
                    contract_type="AUTO",
                    review_attitude="NEUTRAL",
                    schema_version="1.0",
                )
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

        generation = repository.reserve_parse_generation(
            generation_id=f"generation-{marker}-1",
            document_id=document_id,
            tenant_id=tenant_id,
            parser_version="contract-parser-v1",
        )
        assert generation.reused is False
        assert generation.generation_no == 1

        in_progress = repository.reserve_parse_generation(
            generation_id=f"ignored-{marker}",
            document_id=document_id,
            tenant_id=tenant_id,
            parser_version="contract-parser-v1",
        )
        assert in_progress.generation_id == generation.generation_id
        assert in_progress.reused is True
        assert in_progress.completed is False

        blocks = [
            DocumentBlockCreate(
                block_id=f"block-{marker}-1",
                block_no=1,
                block_type="heading",
                text="第一条",
                page_number=1,
                paragraph_no=1,
                char_start=0,
                char_end=3,
                heading_path=["第一条"],
                metadata={},
            ),
            DocumentBlockCreate(
                block_id=f"block-{marker}-2",
                block_no=2,
                block_type="paragraph",
                text="乙方承担付款义务。",
                page_number=1,
                paragraph_no=2,
                char_start=4,
                char_end=13,
                heading_path=["第一条"],
                metadata={"style": "Normal"},
            ),
        ]
        contract_ir = {"document": {"id": document_id}, "clauses": []}
        completed, reused = repository.complete_parse_generation(
            generation_id=generation.generation_id,
            document_id=document_id,
            tenant_id=tenant_id,
            blocks=blocks,
            contract_ir=contract_ir,
        )
        assert reused is False
        assert completed["status"] == "SUCCEEDED"
        assert completed["contract_ir_json"] == contract_ir

        active = repository.get_active_generation(document_id, tenant_id=tenant_id)
        assert active is not None
        assert active["id"] == generation.generation_id
        assert active["contract_ir_json"] == contract_ir
        saved_blocks = repository.list_blocks(generation.generation_id, tenant_id=tenant_id)
        assert [item["block_no"] for item in saved_blocks] == [1, 2]
        assert saved_blocks[1]["text"] == "乙方承担付款义务。"

        completed_again, reused = repository.complete_parse_generation(
            generation_id=generation.generation_id,
            document_id=document_id,
            tenant_id=tenant_id,
            blocks=blocks,
            contract_ir=contract_ir,
        )
        assert reused is True
        assert completed_again["ir_hash"] == completed["ir_hash"]

        reused_generation = repository.reserve_parse_generation(
            generation_id=f"ignored-complete-{marker}",
            document_id=document_id,
            tenant_id=tenant_id,
            parser_version="contract-parser-v1",
        )
        assert reused_generation.generation_id == generation.generation_id
        assert reused_generation.completed is True

        failed = repository.reserve_parse_generation(
            generation_id=f"generation-{marker}-2",
            document_id=document_id,
            tenant_id=tenant_id,
            parser_version="contract-parser-v2",
        )
        assert not repository.fail_parse_generation(
            failed.generation_id,
            tenant_id="another-tenant",
            error_code="CONTRACT_PARSE_FAILED",
        )
        assert repository.fail_parse_generation(
            failed.generation_id,
            tenant_id=tenant_id,
            error_code="CONTRACT_PARSE_FAILED",
        )
        assert repository.get_active_generation(document_id, tenant_id=tenant_id)["id"] == generation.generation_id

        retried = repository.reserve_parse_generation(
            generation_id=f"generation-{marker}-3",
            document_id=document_id,
            tenant_id=tenant_id,
            parser_version="contract-parser-v2",
        )
        assert retried.generation_no == 3
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("DELETE FROM contract_review_run WHERE tenant_id = %s", (tenant_id,))
            conn.execute("DELETE FROM contract_document WHERE tenant_id = %s", (tenant_id,))
            conn.commit()
