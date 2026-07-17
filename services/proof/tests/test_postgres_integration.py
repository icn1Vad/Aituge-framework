from __future__ import annotations

import hashlib
import os
import uuid

import psycopg
import pytest

from proof.application.service import ProofService
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import EmbeddingProfile


DATABASE_URL = os.getenv("PROOF_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_ingest_read_fetch_duplicate_and_atomic_failure(tmp_path) -> None:
    service = ProofService(Settings(database_url=DATABASE_URL, storage_root=tmp_path))
    marker = uuid.uuid4().hex
    content = f"""测试制度 {marker}
第一章 总则
第一条 第一段。
第二段。
第二章 附则
第一条 重复编号也必须成为独立条款。
""".encode()
    policy_id = ""
    run_ids: list[str] = []
    try:
        created = service.ingest_policy(
            content=content,
            filename="integration.txt",
            level_code="peer",
            category_code="general",
        )
        policy_id = created["policy"]["id"]
        run_ids.append(created["ingestion_run_id"])
        assert created["reused"] is False
        assert created["document"]["structure_profile"] == "article"
        assert [item["clause_ordinal"] for item in created["clauses"]] == [1, 2]
        assert [item["clause_no_raw"] for item in created["clauses"]] == ["第一条", "第一条"]

        clauses = service.list_clauses(policy_id, include_text=True)
        assert clauses[0]["text"] == "第一条 第一段。\n第二段。"
        assert service.fetch_units([clauses[0]["id"]]) == []
        assert created["policy"]["status"] == "draft"
        confirmed = service.confirm_policy(policy_id)
        assert confirmed["status"] == "effective"
        fetched = service.fetch_units([clauses[0]["id"]])[0]
        assert fetched["citation"]["policy_id"] == policy_id
        assert fetched["citation"]["clause_no_raw"] == "第一条"

        document_id = created["document"]["id"]
        succeeded_run = service.get_ingestion_run(created["ingestion_run_id"])
        assert succeeded_run["status"] == "succeeded"
        assert succeeded_run["stage"] == "complete"
        assert succeeded_run["reused"] is False
        assert succeeded_run["policy_id"] == policy_id
        assert succeeded_run["document_id"] == document_id
        assert succeeded_run["block_count"] == 6
        assert succeeded_run["clause_count"] == 2
        assert succeeded_run["finished_at"] is not None
        assert succeeded_run["clause_profile"] == "article"

        units = service.repository.get_document_units(document_id)
        profile = EmbeddingProfile(id="integration-3d", provider="test", model="test", dimensions=3)
        service.repository.replace_embeddings(
            document_id,
            [units[0]],
            [[1.0, 0.0, 0.0]],
            profile,
            too_long_unit_ids=[units[1]["id"]],
        )
        statuses = service.list_clauses(policy_id)
        assert [item["unit_type"] for item in statuses] == ["article", "article"]
        assert [item["embedding_status"] for item in statuses] == ["indexed", "embedding_too_long"]
        search_results = service.repository.vector_search(
            query_vector=[1.0, 0.0, 0.0],
            profile=profile,
            top_k=5,
            policy_ids=[],
            level_codes=["peer"],
            category_codes=["general"],
        )
        assert [item["id"] for item in search_results] == [units[0]["id"]]
        assert search_results[0]["score"] == pytest.approx(1.0)

        reused = service.ingest_policy(content=content, filename="renamed.txt")
        run_ids.append(reused["ingestion_run_id"])
        assert reused["reused"] is True
        assert reused["policy"]["id"] == policy_id
        assert reused["ingestion_run_id"] != created["ingestion_run_id"]
        reused_run = service.get_ingestion_run(reused["ingestion_run_id"])
        assert reused_run["status"] == "succeeded"
        assert reused_run["stage"] == "complete"
        assert reused_run["reused"] is True
        assert reused_run["policy_id"] == policy_id
        assert reused_run["document_id"] == document_id
        assert reused_run["block_count"] == succeeded_run["block_count"]
        assert reused_run["clause_count"] == succeeded_run["clause_count"]

        before = _row_counts()
        with pytest.raises(ProofError) as exc_info:
            service.ingest_policy(content=f"title only {marker}\nno article".encode(), filename="invalid.txt")
        assert exc_info.value.code == "no_clauses_found"
        split_run_id = exc_info.value.details["ingestion_run_id"]
        run_ids.append(split_run_id)
        split_run = service.get_ingestion_run(split_run_id)
        assert split_run["status"] == "failed"
        assert split_run["stage"] == "split"
        assert split_run["error_code"] == "no_clauses_found"
        assert split_run["block_count"] == 2
        assert split_run["clause_count"] == 0
        assert _row_counts() == before

        with pytest.raises(ProofError) as exc_info:
            service.ingest_policy(content=b"not a pdf", filename=f"invalid-{marker}.pdf")
        assert exc_info.value.code == "document_parse_failed"
        parse_run_id = exc_info.value.details["ingestion_run_id"]
        run_ids.append(parse_run_id)
        parse_run = service.get_ingestion_run(parse_run_id)
        assert parse_run["status"] == "failed"
        assert parse_run["stage"] == "parse"
        assert parse_run["error_code"] == "document_parse_failed"
        assert _row_counts() == before

        persist_content = f"第一条 持久化失败必须清理原文件。{marker}".encode()
        persist_hash = hashlib.sha256(persist_content).hexdigest()
        persisted_file = tmp_path / "files" / persist_hash[:2] / f"{persist_hash}.txt"
        with pytest.raises(ProofError) as exc_info:
            service.ingest_policy(
                content=persist_content,
                filename=f"persist-{marker}.txt",
                category_code="missing-category",
            )
        assert exc_info.value.code == "invalid_category"
        persist_run_id = exc_info.value.details["ingestion_run_id"]
        run_ids.append(persist_run_id)
        persist_run = service.get_ingestion_run(persist_run_id)
        assert persist_run["status"] == "failed"
        assert persist_run["stage"] == "persist"
        assert persist_run["error_code"] == "invalid_category"
        assert not persisted_file.exists()
        assert _row_counts() == before

        run_count = _run_count()
        with pytest.raises(ProofError) as exc_info:
            service.ingest_policy(content=b"", filename="empty.txt")
        assert exc_info.value.code == "empty_document"
        assert "ingestion_run_id" not in exc_info.value.details
        assert _run_count() == run_count

        with pytest.raises(ProofError) as exc_info:
            service.get_ingestion_run("unknown-run")
        assert exc_info.value.code == "ingestion_run_not_found"
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            if policy_id:
                conn.execute("DELETE FROM proof_policy WHERE id = %s", (policy_id,))
            if run_ids:
                conn.execute("DELETE FROM proof_ingestion_run WHERE id = ANY(%s)", (run_ids,))
            conn.commit()


def _row_counts() -> tuple[int, int, int, int]:
    with psycopg.connect(DATABASE_URL) as conn:
        return tuple(
            conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM proof_policy),
                  (SELECT count(*) FROM proof_document),
                  (SELECT count(*) FROM proof_document_block),
                  (SELECT count(*) FROM proof_retrieval_unit)
                """
            ).fetchone()
        )


def _run_count() -> int:
    with psycopg.connect(DATABASE_URL) as conn:
        return int(conn.execute("SELECT count(*) FROM proof_ingestion_run").fetchone()[0])
