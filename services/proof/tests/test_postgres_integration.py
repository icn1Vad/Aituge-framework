from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from proof.application.service import ProofService
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import EmbeddingProfile
from proof.tenant import tenant_scope, tenant_storage_key

DATABASE_URL = os.getenv("PROOF_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_concurrent_identical_creates_leave_one_policy(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    content = f"并发重复上传 {marker}\n第一条 同一内容只能创建一份制度。".encode()
    content_hash = hashlib.sha256(content).hexdigest()
    barrier = Barrier(2)

    def create(suffix: str) -> tuple[str, str]:
        with tenant_scope("1"):
            barrier.wait()
            try:
                result = service.ingest_policy(
                    content=content,
                    filename=f"concurrent-{suffix}.txt",
                    category_code="other",
                    idempotency_key=f"concurrent-{marker}-{suffix}",
                )
                return ("created", result["policy"]["id"])
            except ProofError as exc:
                return ("error", exc.code)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, ("a", "b")))
        assert sorted(status for status, _ in results) == ["created", "error"]
        assert [value for status, value in results if status == "error"] == [
            "policy_exact_duplicate"
        ]
        with psycopg.connect(DATABASE_URL) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM proof_document
                WHERE tenant_id = '1' AND content_hash = %s
                """,
                (content_hash,),
            ).fetchone()[0]
        assert count == 1
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                """
                DELETE FROM proof_policy
                WHERE tenant_id = '1'
                  AND id IN (
                    SELECT policy_id FROM proof_document
                    WHERE tenant_id = '1' AND content_hash = %s
                  )
                """,
                (content_hash,),
            )
            conn.execute(
                "DELETE FROM proof_ingestion_run WHERE tenant_id = '1' AND content_hash = %s",
                (content_hash,),
            )
            conn.execute(
                """
                DELETE FROM proof_policy_create_request
                WHERE tenant_id = '1' AND idempotency_key LIKE %s
                """,
                (f"concurrent-{marker}-%",),
            )
            conn.commit()


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_concurrent_audit_dispatch_claim_has_one_winner(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    content = f"并发审查调度 {marker}\n第一条 同一审查只能调度一次。".encode()
    content_hash = hashlib.sha256(content).hexdigest()
    barrier = Barrier(2)
    policy_id = ""

    try:
        with tenant_scope("1"):
            created = service.ingest_policy(
                content=content,
                filename="audit-claim.txt",
                category_code="other",
                idempotency_key=f"audit-claim-{marker}",
            )
            policy_id = created["policy"]["id"]
            audit = service.repository.create_audit_run(
                audit_run_id=uuid.uuid4().hex,
                document_id=created["document"]["id"],
            )

        def claim() -> bool:
            with tenant_scope("1"):
                barrier.wait()
                return service.repository.prepare_audit_run_for_dispatch(audit["id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))

        assert sorted(claims) == [False, True]
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            if policy_id:
                conn.execute(
                    "DELETE FROM proof_policy WHERE tenant_id = '1' AND id = %s",
                    (policy_id,),
                )
            conn.execute(
                "DELETE FROM proof_ingestion_run WHERE tenant_id = '1' AND content_hash = %s",
                (content_hash,),
            )
            conn.execute(
                """
                DELETE FROM proof_policy_create_request
                WHERE tenant_id = '1' AND idempotency_key = %s
                """,
                (f"audit-claim-{marker}",),
            )
            conn.commit()


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_ingest_read_fetch_duplicate_and_atomic_failure(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
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
            category_code="other",
            idempotency_key=f"create-{marker}",
        )
        policy_id = created["policy"]["id"]
        run_ids.append(created["ingestion_run_id"])
        assert created["reused"] is False
        assert created["policy"]["normalized_title"] == "integration"
        assert created["policy"]["level"] == {
            "code": "peer",
            "name": "二级制度",
            "sort_rank": 200,
        }
        assert created["policy"]["category"]["code"] == "other"
        assert created["policy"]["category"]["parent"] is None
        assert created["policy"]["category"]["path_name"] == "其他制度"
        assert created["document"]["structure_profile"] == "article"
        assert [item["clause_ordinal"] for item in created["clauses"]] == [1, 2]
        assert [item["clause_no_raw"] for item in created["clauses"]] == ["第一条", "第一条"]

        clauses = service.list_clauses(policy_id, include_text=True)
        assert clauses[0]["text"] == "第一条 第一段。\n第二段。"
        assert service.fetch_units([clauses[0]["id"]]) == []
        assert created["policy"]["status"] == "draft"
        listed = service.list_policies(level_code="peer", category_code="other")
        listed_policy = next(item for item in listed if item["id"] == policy_id)
        assert listed_policy["level"] == created["policy"]["level"]
        assert listed_policy["category"] == created["policy"]["category"]
        confirmed = service.repository.confirm_policy(policy_id)
        assert confirmed["status"] == "effective"
        fetched = service.fetch_units([clauses[0]["id"]])[0]
        assert set(fetched) == {"id", "text", "citation"}
        assert fetched["text"] == clauses[0]["text"]
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
            category_codes=["other"],
        )
        assert [item["id"] for item in search_results] == [units[0]["id"]]
        assert search_results[0]["score"] == pytest.approx(1.0)
        assert service.repository.vector_search(
            query_vector=[1.0, 0.0, 0.0],
            profile=profile,
            top_k=5,
            policy_ids=[],
            level_codes=[],
            category_codes=[],
            excluded_policy_ids=[policy_id],
        ) == []

        with pytest.raises(ProofError) as duplicate:
            service.ingest_policy(
                content=content,
                filename="renamed.txt",
                idempotency_key=f"duplicate-{marker}",
            )
        assert duplicate.value.code == "policy_exact_duplicate"

        before = _row_counts()
        with pytest.raises(ProofError) as exc_info:
            service.ingest_policy(
                content=f"title only {marker}\nno article".encode(),
                filename="invalid.txt",
                idempotency_key=f"invalid-structure-{marker}",
            )
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
            service.ingest_policy(
                content=b"not a pdf",
                filename=f"invalid-{marker}.pdf",
                idempotency_key=f"invalid-pdf-{marker}",
            )
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
        persisted_file = (
            tmp_path
            / "tenants"
            / tenant_storage_key("1")
            / "files"
            / persist_hash[:2]
            / f"{persist_hash}.txt"
        )
        with pytest.raises(ProofError) as exc_info:
            service.ingest_policy(
                content=persist_content,
                filename=f"persist-{marker}.txt",
                category_code="missing-category",
                idempotency_key=f"invalid-category-{marker}",
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
            service.ingest_policy(
                content=b"",
                filename="empty.txt",
                idempotency_key=f"empty-{marker}",
            )
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


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_same_file_is_isolated_between_tenants_one_and_two(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    content = f"双租户隔离 {marker}\n第一条 相同制度必须按租户隔离。".encode()
    policies: list[str] = []
    runs: list[str] = []
    try:
        with tenant_scope("1"):
            main = service.ingest_policy(
                content=content,
                filename="same-policy.txt",
                category_code="other",
                idempotency_key=f"tenant-1-{marker}",
            )
            policies.append(main["policy"]["id"])
            runs.append(main["ingestion_run_id"])
            service.repository.confirm_policy(main["policy"]["id"])
            main_units = service.list_clauses(main["policy"]["id"], include_text=True)
            main_audit_id = uuid.uuid4().hex
            service.repository.create_audit_run(
                audit_run_id=main_audit_id,
                document_id=main["document"]["id"],
            )

        with tenant_scope("2"):
            assert service.repository.get_audit_run(main_audit_id) is None
            with pytest.raises(ProofError) as hidden_callback:
                service.repository.complete_audit(main_audit_id, [])
            assert hidden_callback.value.code == "audit_run_not_found"
            with pytest.raises(ProofError) as hidden:
                service.get_policy(main["policy"]["id"])
            assert hidden.value.code == "policy_not_found"
            demo = service.ingest_policy(
                content=content,
                filename="same-policy.txt",
                category_code="other",
                idempotency_key=f"tenant-2-{marker}",
            )
            policies.append(demo["policy"]["id"])
            runs.append(demo["ingestion_run_id"])
            service.repository.confirm_policy(demo["policy"]["id"])
            demo_units = service.list_clauses(demo["policy"]["id"], include_text=True)
            demo_audit_id = uuid.uuid4().hex
            service.repository.create_audit_run(
                audit_run_id=demo_audit_id,
                document_id=demo["document"]["id"],
            )

            assert demo["reused"] is False
            assert main["policy"]["version"] == "v1.0.0"
            assert demo["policy"]["version"] == "v1.0.0"
            assert demo["similarity"]["status"] == "clear"
            assert demo["policy"]["id"] != main["policy"]["id"]
            assert demo["document"]["id"] != main["document"]["id"]
            assert demo["document"]["content_hash"] == main["document"]["content_hash"]
            assert demo["document"]["storage_path"] != main["document"]["storage_path"]
            assert (tmp_path / demo["document"]["storage_path"]).is_file()
            assert service.fetch_units([main_units[0]["id"]]) == []
            assert [item["id"] for item in service.fetch_units([demo_units[0]["id"]])] == [
                demo_units[0]["id"]
            ]
            sql_result = service.execute_sql(
                question="验证测试租户制度",
                sql=(
                    "SELECT policy_id FROM proof_sql_policy_v "
                    f"WHERE policy_id IN ('{main['policy']['id']}', '{demo['policy']['id']}')"
                ),
            )
            assert [row["policy_id"] for row in sql_result["rows"]] == [demo["policy"]["id"]]

        with tenant_scope("1"):
            assert (tmp_path / main["document"]["storage_path"]).is_file()
            assert service.repository.get_audit_run(demo_audit_id) is None
            with pytest.raises(ProofError) as hidden_callback:
                service.repository.complete_audit(demo_audit_id, [])
            assert hidden_callback.value.code == "audit_run_not_found"
            assert service.fetch_units([demo_units[0]["id"]]) == []
            assert [item["id"] for item in service.fetch_units([main_units[0]["id"]])] == [
                main_units[0]["id"]
            ]
            with pytest.raises(ProofError) as hidden_run:
                service.get_ingestion_run(demo["ingestion_run_id"])
            assert hidden_run.value.code == "ingestion_run_not_found"
            sql_result = service.execute_sql(
                question="验证主租户制度",
                sql=(
                    "SELECT policy_id FROM proof_sql_policy_v "
                    f"WHERE policy_id IN ('{main['policy']['id']}', '{demo['policy']['id']}')"
                ),
            )
            assert [row["policy_id"] for row in sql_result["rows"]] == [main["policy"]["id"]]
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            if policies:
                conn.execute("DELETE FROM proof_policy WHERE id = ANY(%s)", (policies,))
            if runs:
                conn.execute("DELETE FROM proof_ingestion_run WHERE id = ANY(%s)", (runs,))
            conn.commit()


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_similarity_version_switch_retires_old_embeddings_and_detects_stale_base(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    title = f"采购版本升级验收 {marker}"
    old_text = "\n".join(
        [
            f"{title}",
            "第一条 采购申请应当由部门负责人审批。",
            "第二条 采购金额超过十万元应当集体决策。",
            "第三条 采购资料保存期限为十年。",
            "第四条 供应商应当完成准入审查。",
            "第五条 采购结果应当及时归档。",
        ]
    )
    policy_ids: list[str] = []
    run_ids: list[str] = []
    profile = EmbeddingProfile(
        id="similarity-version-3d",
        provider="test",
        model="test",
        dimensions=3,
    )
    try:
        old = service.ingest_policy(
            content=old_text.encode(),
            filename="policy-v1.txt",
            title=title,
            category_code="procurement_supply",
            idempotency_key=f"upgrade-base-{marker}",
        )
        policy_ids.append(old["policy"]["id"])
        run_ids.append(old["ingestion_run_id"])
        service.repository.confirm_policy(old["policy"]["id"])
        old_units = service.repository.get_document_units(old["document"]["id"])
        service.repository.replace_embeddings(
            old["document"]["id"],
            old_units,
            [[1.0, 0.0, 0.0] for _ in old_units],
            profile,
        )

        first_upgrade_content = old_text.replace("十万元", "十二万元").encode()
        first_upgrade = service.ingest_policy(
            content=first_upgrade_content,
            filename="policy-v2.txt",
            title=f"{title}（修订）",
            category_code="procurement_supply",
            similarity_decision="new_version",
            candidate_policy_id=old["policy"]["id"],
            idempotency_key=f"upgrade-one-{marker}",
        )
        policy_ids.append(first_upgrade["policy"]["id"])
        run_ids.append(first_upgrade["ingestion_run_id"])
        assert first_upgrade["policy"]["version"] == "v1.0.1"
        assert first_upgrade["policy"]["version_seq"] == 1
        assert first_upgrade["policy"]["title"] == title
        retried_one = service.ingest_policy(
            content=first_upgrade_content,
            filename="policy-v2.txt",
            title=f"{title}（修订）",
            category_code="procurement_supply",
            similarity_decision="new_version",
            candidate_policy_id=old["policy"]["id"],
            idempotency_key=f"upgrade-one-{marker}",
        )
        assert retried_one["policy"]["version"] == "v1.0.1"
        assert retried_one["policy"]["id"] == first_upgrade["policy"]["id"]
        assert retried_one["idempotency_replay"] is True
        with pytest.raises(ProofError) as idempotency_conflict:
            service.ingest_policy(
                content=first_upgrade_content,
                filename="policy-v2.txt",
                title=f"{title}（修订）",
                category_code="procurement_supply",
                similarity_decision="separate",
                idempotency_key=f"upgrade-one-{marker}",
            )
        assert idempotency_conflict.value.code == "idempotency_conflict"

        second_upgrade = service.ingest_policy(
            content=old_text.replace("十万元", "十三万元").encode(),
            filename="policy-v3.txt",
            title=title,
            category_code="procurement_supply",
            similarity_decision="new_version",
            candidate_policy_id=first_upgrade["policy"]["id"],
            idempotency_key=f"upgrade-two-{marker}",
        )
        policy_ids.append(second_upgrade["policy"]["id"])
        run_ids.append(second_upgrade["ingestion_run_id"])
        assert second_upgrade["policy"]["version"] == "v1.0.2"
        assert second_upgrade["policy"]["version_seq"] == 2
        assert (
            second_upgrade["policy"]["supersedes_policy_id"]
            == first_upgrade["policy"]["id"]
        )

        first_units = service.repository.get_document_units(first_upgrade["document"]["id"])
        service.repository.replace_embeddings(
            first_upgrade["document"]["id"],
            first_units,
            [[0.0, 1.0, 0.0] for _ in first_units],
            profile,
        )
        activated = service.repository.confirm_policy(first_upgrade["policy"]["id"])
        assert activated["status"] == "effective"
        assert service.get_policy(old["policy"]["id"])["status"] == "expired"
        assert {
            item["embedding_status"]
            for item in service.list_clauses(old["policy"]["id"])
        } == {"retired"}
        assert service.repository.vector_search(
            query_vector=[1.0, 0.0, 0.0],
            profile=profile,
            top_k=20,
            policy_ids=[old["policy"]["id"]],
            level_codes=[],
            category_codes=[],
        ) == []

        second_units = service.repository.get_document_units(second_upgrade["document"]["id"])
        service.repository.replace_embeddings(
            second_upgrade["document"]["id"],
            second_units,
            [[0.0, 0.0, 1.0] for _ in second_units],
            profile,
        )
        activated_second = service.repository.confirm_policy(
            second_upgrade["policy"]["id"]
        )
        assert activated_second["status"] == "effective"
        assert service.get_policy(first_upgrade["policy"]["id"])["status"] == "expired"
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            if policy_ids:
                conn.execute("DELETE FROM proof_policy WHERE id = ANY(%s)", (policy_ids,))
            if run_ids:
                conn.execute("DELETE FROM proof_ingestion_run WHERE id = ANY(%s)", (run_ids,))
            conn.commit()


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_discard_draft_cascades_temporary_audit_data(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    policy_id = ""
    ingestion_run_id = ""
    try:
        created = service.ingest_policy(
            content=(
                f"草稿删除级联验收 {marker}\n"
                "第一条 申请必须审批。\n"
                "第二条 申请无需审批。\n"
            ).encode(),
            filename="discard-cascade.txt",
            category_code="other",
            idempotency_key=f"discard-create-{marker}",
        )
        policy_id = created["policy"]["id"]
        ingestion_run_id = created["ingestion_run_id"]
        document_id = created["document"]["id"]
        audit_run_id = uuid.uuid4().hex
        service.repository.create_audit_run(
            audit_run_id=audit_run_id,
            document_id=document_id,
        )
        units = service.repository.get_document_units(document_id)
        profile = EmbeddingProfile(
            id="discard-cascade-3d",
            provider="test",
            model="test",
            dimensions=3,
        )
        service.repository.replace_draft_embeddings(
            audit_run_id,
            document_id,
            units,
            [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0]],
            profile,
        )
        service.repository.complete_intra_conflict_audit(
            audit_run_id,
            [
                {
                    "id": units[0]["id"],
                    "candidate_ids": [units[1]["id"]],
                    "conflict_type": "rule_reversal",
                    "problem": "审批要求相反。",
                    "suggestion": "统一审批要求。",
                }
            ],
        )

        assert _audit_data_counts(audit_run_id) == (1, 2, 1)
        stored_file = tmp_path / created["document"]["storage_path"]
        assert stored_file.is_file()

        discarded = service.execute_policy_action(
            policy_id,
            action="discard",
            idempotency_key=f"discard-{marker}",
        )
        assert discarded["id"] == policy_id
        assert discarded["status"] == "discarded"
        assert discarded["operation_status"] == "SUCCEEDED"
        assert service.execute_policy_action(
            policy_id,
            action="discard",
            idempotency_key=f"discard-{marker}",
        )["operation_status"] == "SUCCEEDED"
        assert _audit_data_counts(audit_run_id) == (0, 0, 0)
        assert service.repository.get_policy(policy_id) is None
        assert not stored_file.exists()
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            if policy_id:
                conn.execute("DELETE FROM proof_policy WHERE id = %s", (policy_id,))
            if ingestion_run_id:
                conn.execute(
                    "DELETE FROM proof_ingestion_run WHERE id = %s",
                    (ingestion_run_id,),
                )
            conn.commit()


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_expire_and_delete_lifecycle_operations_are_tenant_scoped_and_retryable(tmp_path) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    expire_policy_id = ""
    expire_run_id = ""
    delete_policy_id = ""
    delete_run_id = ""
    operation_ids = [f"expire-{marker}", f"delete-{marker}"]
    try:
        with tenant_scope("1"):
            expiring = service.ingest_policy(
                content=f"过期验收 {marker}\n第一条 仍保留历史。".encode(),
                filename="expire-policy.txt",
                category_code="other",
                idempotency_key=f"expire-create-{marker}",
            )
            expire_policy_id = expiring["policy"]["id"]
            expire_run_id = expiring["ingestion_run_id"]
            service.repository.confirm_policy(expire_policy_id)
            units = service.repository.get_document_units(expiring["document"]["id"])
            profile = EmbeddingProfile(
                id="lifecycle-3d",
                provider="test",
                model="test",
                dimensions=3,
            )
            service.repository.replace_embeddings(
                expiring["document"]["id"],
                units,
                [[1.0, 0.0, 0.0] for _ in units],
                profile,
            )

            expired = service.apply_policy_action(
                policy_id=expire_policy_id,
                action="expire",
                operation_id=operation_ids[0],
            )
            assert expired["status"] == "expired"
            assert {item["embedding_status"] for item in service.list_clauses(expire_policy_id)} == {
                "retired"
            }
            assert service.repository.vector_search(
                query_vector=[1.0, 0.0, 0.0],
                profile=profile,
                top_k=5,
                policy_ids=[expire_policy_id],
                level_codes=[],
                category_codes=[],
            ) == []

            deleting = service.ingest_policy(
                content=(
                    f"删除验收 {marker}\n"
                    "第一条 永久删除检索证据仅用于删除后问答失效验收。"
                ).encode(),
                filename="delete-policy.txt",
                category_code="other",
                similarity_decision="separate",
                idempotency_key=f"delete-create-{marker}",
            )
            delete_policy_id = deleting["policy"]["id"]
            delete_run_id = deleting["ingestion_run_id"]
            source_path = tmp_path / deleting["document"]["storage_path"]
            service.repository.confirm_policy(delete_policy_id)
            delete_units = service.repository.get_document_units(deleting["document"]["id"])
            service.repository.replace_embeddings(
                deleting["document"]["id"],
                delete_units,
                [[0.0, 1.0, 0.0] for _ in delete_units],
                profile,
            )
            assert service.fetch_units([delete_units[0]["id"]])
            assert delete_policy_id in [
                item["policy_id"]
                for item in service.search(
                    query="永久删除检索证据",
                    retrieval_mode="keyword",
                )["results"]
            ]

            deleted = service.apply_policy_action(
                policy_id=delete_policy_id,
                action="delete",
                operation_id=operation_ids[1],
            )
            assert deleted["status"] == "deleted"
            assert not source_path.exists()
            assert service.repository.get_policy(delete_policy_id) is None
            assert service.repository.get_ingestion_run(delete_run_id) is None
            assert service.repository.get_delete_tombstone(operation_ids[1])["status"] == "deleted"
            assert service.fetch_units([delete_units[0]["id"]]) == []
            assert delete_policy_id not in {
                item["policy_id"]
                for item in service.search(
                    query="永久删除检索证据",
                    retrieval_mode="keyword",
                )["results"]
            }
            assert service.repository.vector_search(
                query_vector=[0.0, 1.0, 0.0],
                profile=profile,
                top_k=5,
                policy_ids=[delete_policy_id],
                level_codes=[],
                category_codes=[],
            ) == []
            assert delete_policy_id not in {
                item["id"] for item in service.list_policies()
            }

            retried = service.apply_policy_action(
                policy_id=delete_policy_id,
                action="delete",
                operation_id=operation_ids[1],
            )
            assert retried["status"] == "deleted"
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            if expire_policy_id:
                conn.execute("DELETE FROM proof_policy WHERE id = %s", (expire_policy_id,))
            if expire_run_id:
                conn.execute("DELETE FROM proof_ingestion_run WHERE id = %s", (expire_run_id,))
            if delete_policy_id:
                conn.execute("DELETE FROM proof_policy WHERE id = %s", (delete_policy_id,))
            if delete_run_id:
                conn.execute("DELETE FROM proof_ingestion_run WHERE id = %s", (delete_run_id,))
            conn.execute(
                "DELETE FROM proof_policy_lifecycle_operation WHERE operation_id = ANY(%s)",
                (operation_ids,),
            )
            conn.execute(
                "DELETE FROM proof_policy_delete_tombstone WHERE operation_id = ANY(%s)",
                (operation_ids,),
            )
            conn.commit()


@pytest.mark.skipif(not DATABASE_URL, reason="PROOF_TEST_DATABASE_URL is not configured")
def test_lifecycle_claim_is_concurrent_idempotent_and_failed_attempt_is_retryable(
    tmp_path,
) -> None:
    service = ProofService(
        Settings(
            database_url=DATABASE_URL,
            storage_root=tmp_path,
            semantic_audit_enabled=False,
        )
    )
    marker = uuid.uuid4().hex
    operation_id = f"claim-{marker}"
    policy_id = f"policy-{marker}"
    idempotency_key = f"request-{marker}"
    fingerprint = hashlib.sha256(b"expire:false").hexdigest()
    barrier = Barrier(2)

    def claim() -> tuple[bool, dict]:
        with tenant_scope("1"):
            barrier.wait()
            return service.repository.claim_policy_operation(
                operation_id=operation_id,
                policy_id=policy_id,
                action="expire",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))

        assert sorted(claimed for claimed, _ in claims) == [False, True]
        assert {operation["operation_id"] for _, operation in claims} == {
            operation_id
        }
        assert {operation["attempt_count"] for _, operation in claims} == {1}

        with tenant_scope("1"):
            service.repository.complete_policy_operation(
                operation_id,
                status="FAILED",
                error_message="temporary failure",
            )
            retried, operation = service.repository.claim_policy_operation(
                operation_id=operation_id,
                policy_id=policy_id,
                action="expire",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )

            assert retried is True
            assert operation["attempt_count"] == 2
            assert operation["status"] == "ACCEPTED"

            with pytest.raises(ProofError) as exc_info:
                service.repository.claim_policy_operation(
                    operation_id=f"different-{marker}",
                    policy_id=policy_id,
                    action="delete",
                    idempotency_key=idempotency_key,
                    request_fingerprint=hashlib.sha256(b"delete:false").hexdigest(),
                )
            assert exc_info.value.code == "idempotency_conflict"
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "DELETE FROM proof_policy_lifecycle_operation WHERE operation_id = %s",
                (operation_id,),
            )
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


def _audit_data_counts(audit_run_id: str) -> tuple[int, int, int]:
    with psycopg.connect(DATABASE_URL) as conn:
        return tuple(
            conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM proof_audit_run WHERE id = %s),
                  (SELECT count(*) FROM proof_draft_retrieval_embedding
                   WHERE audit_run_id = %s),
                  (SELECT count(*) FROM proof_intra_conflict_audit_finding
                   WHERE audit_run_id = %s)
                """,
                (audit_run_id, audit_run_id, audit_run_id),
            ).fetchone()
        )
