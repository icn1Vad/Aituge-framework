from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx
import pytest
from contract.config import Settings
from contract.legal_evidence.embedding import OpenAICompatibleLegalEmbeddingProvider
from contract.legal_evidence.indexer import LegalEvidenceIndexer
from contract.legal_evidence.models import (
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalRetrievalUnit,
)
from contract.legal_evidence.planner import AdaptiveLegalEvidencePlanner
from contract.legal_evidence.postgres_repository import PostgresLegalEvidenceRepository
from contract.legal_evidence.projection import (
    LegalArticleAssembler,
    extract_internal_references,
    extract_named_instrument_references,
    unique_instrument_aliases,
)
from contract.legal_evidence.testing import InMemoryLegalEvidenceRepository
from contract.persistence.postgres.migrate import MIGRATIONS_DIR


def test_migration_defines_independent_projection_tables_and_ann_index() -> None:
    sql = (MIGRATIONS_DIR / "006_legal_evidence_projection.sql").read_text("utf-8")

    for table in (
        "legal_evidence_release",
        "legal_evidence_unit",
        "legal_evidence_embedding",
        "legal_evidence_relation",
        "legal_evidence_projection_checkpoint",
        "legal_evidence_plan_snapshot",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "USING gin (search_vector)" in sql
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "metadata_verification_status" in sql
    assert "effective_from date NULL" in sql
    assert "effective_to date NULL" in sql
    resume_sql = (MIGRATIONS_DIR / "008_legal_evidence_resume_hashes.sql").read_text(
        "utf-8"
    )
    assert "projection_hash char(64)" in resume_sql
    assert resume_sql.count("embedding_input_hash char(64)") == 2
    checkpoint_sql = (
        MIGRATIONS_DIR / "010_legal_evidence_resumable_checkpoint.sql"
    ).read_text("utf-8")
    assert "ADD PRIMARY KEY (projection_release_id)" in checkpoint_sql
    assert "idx_legal_projection_checkpoint_source" in checkpoint_sql


def test_projection_and_embedding_hashes_cover_their_exact_inputs() -> None:
    base = LegalRetrievalUnit(
        unit_id="unit-1",
        release_id="release-1",
        instrument_id="instrument-1",
        version_id="version-1",
        source_node_ids=["node-1"],
        title="测试法规",
        article_no="第一条",
        content="相同正文。",
        jurisdiction="CN",
        content_hash="a" * 64,
        sequence=1,
    )
    renamed = base.model_copy(update={"title": "测试法规修订版"})
    moved = base.model_copy(update={"jurisdiction": "CN-11", "sequence": 2})

    assert renamed.content_hash == base.content_hash
    assert renamed.embedding_input_hash != base.embedding_input_hash
    assert renamed.projection_hash != base.projection_hash
    assert moved.embedding_input_hash == base.embedding_input_hash
    assert moved.projection_hash != base.projection_hash


def test_repository_resume_rejects_projection_and_embedding_input_drift() -> None:
    base = LegalRetrievalUnit(
        unit_id="unit-1",
        release_id="release-1",
        instrument_id="instrument-1",
        version_id="version-1",
        source_node_ids=["node-1"],
        title="测试法规",
        article_no="第一条",
        content="相同正文。",
        content_hash="a" * 64,
        sequence=1,
    )

    class _Connection:
        def execute(self, sql, _params):
            if "FROM legal_evidence_embedding" in sql:
                return _RowsCursor(
                    [
                        {
                            "unit_id": base.unit_id,
                            "embedding_input_hash": base.embedding_input_hash,
                        }
                    ]
                )
            if "FROM legal_evidence_unit" in sql:
                return _RowsCursor(
                    [
                        {
                            "unit_id": base.unit_id,
                            "projection_hash": base.projection_hash,
                        }
                    ]
                )
            raise AssertionError(sql)

    class _Repository(PostgresLegalEvidenceRepository):
        def __init__(self):
            super().__init__(Settings())
            self.connection = _Connection()

        @contextmanager
        def _connect(self):
            yield self.connection

    repository = _Repository()
    assert repository.units_requiring_projection([base]) == []
    assert repository.units_requiring_embedding([base], profile_id="profile") == []

    renamed = base.model_copy(update={"title": "测试法规修订版"})
    with pytest.raises(RuntimeError, match="projection unit identity drifted"):
        repository.units_requiring_projection([renamed])
    with pytest.raises(RuntimeError, match="embedding input hash drifted"):
        repository.units_requiring_embedding([renamed], profile_id="profile")


def test_resume_cursor_prefers_last_complete_checkpoint_over_newer_unit_row() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, sql, _params):
            self.calls += 1
            assert "legal_evidence_projection_checkpoint" in sql
            return _Cursor(
                {
                    "version_id": "version-checkpointed",
                    "sequence": 18,
                    "unit_count": 21,
                    "embedding_count": 19,
                    "missing_version_id": "version-checkpointed",
                    "missing_sequence": 7,
                }
            )

    class _Repository(PostgresLegalEvidenceRepository):
        def __init__(self):
            super().__init__(Settings())
            self.connection = _Connection()

        @contextmanager
        def _connect(self):
            yield self.connection

    repository = _Repository()

    assert repository.projection_resume_cursor("projection-1") == (
        "version-checkpointed",
        18,
        21,
        19,
    )
    assert repository.connection.calls == 1


def test_resume_cursor_rewinds_to_embedding_gap_before_checkpoint() -> None:
    class _Connection:
        def execute(self, sql, _params):
            assert "e.unit_id IS NULL" in sql
            return _Cursor(
                {
                    "version_id": "version-020",
                    "sequence": 18,
                    "unit_count": 21,
                    "embedding_count": 19,
                    "missing_version_id": "version-010",
                    "missing_sequence": 7,
                }
            )

    class _Repository(PostgresLegalEvidenceRepository):
        def __init__(self):
            super().__init__(Settings())
            self.connection = _Connection()

        @contextmanager
        def _connect(self):
            yield self.connection

    assert _Repository().projection_resume_cursor("projection-1") == (
        "version-010",
        7,
        21,
        19,
    )


def test_legacy_resume_cursor_rewinds_to_gap_when_no_checkpoint_exists() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, sql, _params):
            self.calls += 1
            if self.calls == 1:
                assert "legal_evidence_projection_checkpoint" in sql
                return _Cursor(None)
            assert "incomplete" in sql
            assert "e.unit_id IS NULL" in sql
            return _Cursor(
                {
                    "version_id": "version-020",
                    "sequence": 18,
                    "unit_count": 21,
                    "embedding_count": 19,
                    "missing_version_id": "version-010",
                    "missing_sequence": 7,
                }
            )

    class _Repository(PostgresLegalEvidenceRepository):
        def __init__(self):
            super().__init__(Settings())
            self.connection = _Connection()

        @contextmanager
        def _connect(self):
            yield self.connection

    repository = _Repository()
    assert repository.projection_resume_cursor("projection-1") == (
        "version-010",
        7,
        21,
        19,
    )
    assert repository.connection.calls == 2


def test_article_assembler_keeps_article_and_child_paragraphs_in_one_unit() -> None:
    assembler = LegalArticleAssembler()
    rows = [
        {
            "release_id": "release-1",
            "instrument_key": "instrument-1",
            "version_id": "version-1",
            "title": "测试条例",
            "jurisdiction_code": "CN-TEST",
            "jurisdiction_name": "测试市",
            "issuing_authority_names_json": "[]",
            "source_url": None,
            "node_id": "article-1",
            "parent_node_id": "chapter-1",
            "node_type": "ARTICLE",
            "node_number": "第一条",
            "heading": None,
            "content_plain": "第一条",
            "sequence": 3,
            "path": "第一章/第一条",
            "content_sha256": "a" * 64,
        },
        {
            "release_id": "release-1",
            "instrument_key": "instrument-1",
            "version_id": "version-1",
            "title": "测试条例",
            "jurisdiction_code": "CN-TEST",
            "jurisdiction_name": "测试市",
            "issuing_authority_names_json": "[]",
            "source_url": None,
            "node_id": "paragraph-1",
            "parent_node_id": "article-1",
            "node_type": "PARAGRAPH",
            "node_number": "第一款",
            "heading": None,
            "content_plain": "依照本条例第三条执行。",
            "sequence": 4,
            "path": "第一章/第一条/第一款",
            "content_sha256": "b" * 64,
        },
    ]

    units = []
    for row in rows:
        units.extend(assembler.feed(row))
    units.extend(assembler.finish())

    assert len(units) == 1
    assert units[0].source_node_ids == ["article-1", "paragraph-1"]
    assert units[0].article_no == "第一条"
    assert "依照本条例第三条执行" in units[0].content
    assert units[0].effective_from is None
    assert units[0].validity_status == "UNKNOWN"
    assert units[0].metadata_verification_status == "UNVERIFIED"


def test_internal_reference_extraction_is_deterministic() -> None:
    assert extract_internal_references("按照本法第三条和第十二条办理。") == [
        "第三条",
        "第十二条",
    ]


def test_named_statute_reference_is_classified_and_only_unique_aliases_link() -> None:
    unique, ambiguous = unique_instrument_aliases(
        [
            {
                "instrument_key": "civil-code",
                "title": "中华人民共和国民法典",
                "normalized_title": "民法典",
                "raw_titles_json": '["中华人民共和国民法典"]',
            },
            {
                "instrument_key": "rule-a",
                "title": "实施办法",
                "normalized_title": "实施办法",
                "raw_titles_json": "[]",
            },
            {
                "instrument_key": "rule-b",
                "title": "实施办法",
                "normalized_title": "实施办法",
                "raw_titles_json": "[]",
            },
        ]
    )
    references = extract_named_instrument_references(
        "为了规范合同活动，根据《中华人民共和国民法典》制定本规定。"
    )

    assert unique["中华人民共和国民法典"] == "civil-code"
    assert "实施办法" in ambiguous
    assert references[0].normalized_title == "中华人民共和国民法典"
    assert references[0].relation_type == "BASED_ON"
    assert len(references[0].evidence_text) < 320


def test_named_statute_reference_preserves_explicit_target_article() -> None:
    references = extract_named_instrument_references(
        "本办法依据《中华人民共和国民法典》第五百八十五条制定。"
    )

    assert references[0].target_article_no == "第五百八十五条"


def test_indexer_does_not_depend_on_a_java_export_endpoint() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "contract"
        / "legal_evidence"
        / "mysql_source.py"
    )
    text = source.read_text("utf-8")
    assert "SSDictCursor" in text
    assert "biz_legal_node" in text
    assert "LIMIT %s" in text


def test_authoritative_source_relations_skip_fallback_and_keep_node_mapping() -> None:
    class _Source:
        @staticmethod
        def active_release():
            return {"release_id": "source-1", "manifest_sha256": "a" * 64}

        @staticmethod
        def retrieval_root_count(_release_id):
            return 2

        @staticmethod
        def relation_count(_release_id):
            return 1

        @staticmethod
        def iter_nodes(_release_id, *, page_size):
            del page_size
            for index in (1, 2):
                yield {
                    "release_id": "source-1",
                    "instrument_key": f"instrument-{index}",
                    "version_id": f"version-{index}",
                    "title": f"测试法规{index}",
                    "category_root": "法律",
                    "jurisdiction_code": "CN",
                    "issuing_authority_names_json": "[]",
                    "source_url": None,
                    "node_id": f"node-{index}",
                    "parent_node_id": None,
                    "node_type": "PREAMBLE",
                    "node_number": None,
                    "heading": None,
                    "content_plain": f"测试正文{index}",
                    "sequence": 0,
                    "path": "前言",
                    "content_sha256": str(index) * 64,
                }

        @staticmethod
        def iter_instruments(_release_id, *, page_size):
            del page_size
            raise AssertionError("authoritative releases must skip fallback extraction")

        @staticmethod
        def iter_relations(_release_id, *, page_size):
            del page_size
            yield {
                "relation_id": "source-relation-1",
                "source_instrument_key": "instrument-1",
                "source_version_id": "version-1",
                "source_node_id": "node-1",
                "target_instrument_key": "instrument-2",
                "target_version_id": "version-2",
                "target_node_id": "node-2",
                "relation_type": "CITES",
                "evidence_text": "依据测试法规2制定",
                "evidence_start": 0,
                "evidence_end": 8,
                "evidence_location_json": {},
                "extractor_version": "source-extractor-v1",
                "confidence": 1.0,
                "verification_status": "AUTO_VERIFIED",
            }

    class _Target:
        def __init__(self):
            self.units = {}
            self.relations = []

        @staticmethod
        def active_release():
            return None

        @staticmethod
        def stage_release(**_kwargs):
            return True

        @staticmethod
        def units_requiring_projection(units):
            return list(units)

        def upsert_units(self, units):
            units = list(units)
            self.units.update({item.unit_id: item for item in units})
            return len(units)

        @staticmethod
        def reset_staged_relations(_release_id):
            return None

        def all_unit_ids_by_source_nodes(self, _release_id):
            return {
                node_id: unit.unit_id
                for unit in self.units.values()
                for node_id in unit.source_node_ids
            }

        @staticmethod
        def representative_unit_id(*_args):
            raise AssertionError("exact source node mapping should be used")

        def upsert_relations(self, relations):
            relations = list(relations)
            self.relations.extend(relations)
            return len(relations)

        def mark_projection_complete(self, _release_id, *, expected_unit_count):
            assert expected_unit_count == 2
            return 2, len(self.relations)

    target = _Target()
    result = LegalEvidenceIndexer(_Source(), target, write_batch_size=2).publish()

    assert result["actual_relations"] == 1
    assert len(target.relations) == 1
    relation = target.relations[0]
    assert relation.source_node_id == "node-1"
    assert relation.source_unit_id == next(
        unit.unit_id for unit in target.units.values() if "node-1" in unit.source_node_ids
    )
    assert relation.target_unit_id == next(
        unit.unit_id for unit in target.units.values() if "node-2" in unit.source_node_ids
    )


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _SnapshotConnection:
    def __init__(self) -> None:
        self.saved = None

    def execute(self, sql, params):
        if "INSERT INTO legal_evidence_plan_snapshot" in sql:
            if self.saved is None:
                self.saved = {
                    "request_hash": params[4],
                    "bundle_hash": params[5],
                    "bundle_json": params[6].obj,
                    "status": params[7],
                }
            return _Cursor(None)
        if "SELECT request_hash, bundle_hash" in sql:
            return _Cursor(self.saved)
        raise AssertionError(sql)

    def commit(self):
        return None


class _RowsCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _RelationConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        return _RowsCursor(
            [
                {
                    "relation_id": "relation-inbound",
                    "release_id": "release-1",
                    "source_unit_id": "amending-unit",
                    "target_unit_id": "queried-unit",
                    "relation_type": "AMENDS",
                    "evidence_text": "修改目标条文",
                    "confidence": 1.0,
                    "verification_status": "AUTO_VERIFIED",
                    "target_unit": {
                        "unit_id": "amending-unit",
                        "release_id": "release-1",
                        "instrument_id": "instrument-amendment",
                        "version_id": "version-amendment",
                        "source_node_ids": ["node-amendment"],
                        "title": "修改决定",
                        "article_no": "第一条",
                        "heading_path": ["第一条"],
                        "content": "本决定修改目标条文。",
                        "jurisdiction": "CN",
                        "authority_level": None,
                        "issuing_authority": None,
                        "effective_from": None,
                        "effective_to": None,
                        "validity_status": None,
                        "metadata_verification_status": "UNVERIFIED",
                        "official_source_url": None,
                        "content_hash": "c" * 64,
                        "sequence": 1,
                    },
                }
            ]
        )


class _RelationRepository(PostgresLegalEvidenceRepository):
    def __init__(self) -> None:
        super().__init__(Settings())
        self.connection = _RelationConnection()

    @contextmanager
    def _connect(self):
        yield self.connection


class _ExistingReleaseConnection:
    def __init__(self, *, manifest: str = "a" * 64) -> None:
        self.manifest = manifest
        self.statements: list[str] = []

    def execute(self, sql, _params):
        self.statements.append(sql)
        if "INSERT INTO legal_evidence_release" in sql:
            return _Cursor(None)
        if "FROM legal_evidence_release" in sql:
            return _Cursor(
                {
                    "source_release_id": "mysql-release-1",
                    "source_manifest_sha256": self.manifest,
                    "projection_version": "legal-evidence-projection-v1",
                    "embedding_profile_id": None,
                    "status": "ACTIVE",
                }
            )
        raise AssertionError(sql)

    def commit(self):
        return None


class _ExistingReleaseRepository(PostgresLegalEvidenceRepository):
    def __init__(self, *, manifest: str = "a" * 64) -> None:
        super().__init__(Settings())
        self.connection = _ExistingReleaseConnection(manifest=manifest)

    @contextmanager
    def _connect(self):
        yield self.connection


class _NoopSource:
    def active_release(self):
        return {
            "release_id": "mysql-release-1",
            "manifest_sha256": "a" * 64,
        }

    def retrieval_root_count(self, _release_id):
        return 1


class _NoopPublishedTarget:
    def __init__(self) -> None:
        self.release_id = None

    def stage_release(self, *, release_id, **_kwargs):
        self.release_id = release_id
        return False

    def activate_release(self, _release_id):
        raise AssertionError("activation was not requested")


class _SnapshotRepository(PostgresLegalEvidenceRepository):
    def __init__(self) -> None:
        super().__init__(Settings())
        self.connection = _SnapshotConnection()

    @contextmanager
    def _connect(self):
        yield self.connection


def test_plan_snapshot_write_is_idempotent_and_bundle_hash_is_stable() -> None:
    request = LegalEvidencePlanRequest(
        review_id="review-1",
        generation_id="generation-1",
        contract_type="AUTO",
        jurisdiction="CN",
        review_as_of_date=date(2026, 8, 31),
        issues=[
            LegalEvidenceIssue(
                issue_id="legal-issue-" + "1" * 32,
                domain="commercial_financial",
                query="付款法律规则",
                check_codes=["CF-001"],
            )
        ],
    )
    planner = AdaptiveLegalEvidencePlanner(InMemoryLegalEvidenceRepository())
    first_bundle = planner.plan(request)
    second_bundle = planner.plan(request)
    repository = _SnapshotRepository()

    assert first_bundle.bundle_hash == second_bundle.bundle_hash
    first_snapshot = repository.save_plan_snapshot(
        request=request, bundle=first_bundle, attempt_no=1
    )
    second_snapshot = repository.save_plan_snapshot(
        request=request, bundle=second_bundle, attempt_no=1
    )

    assert first_snapshot.bundle == first_bundle
    assert second_snapshot == first_snapshot


def test_relation_neighbors_expands_inbound_edges_without_reversing_relation() -> None:
    repository = _RelationRepository()

    neighbors = repository.relation_neighbors(
        release_id="release-1",
        unit_id="queried-unit",
    )

    relation, neighbor = neighbors[0]
    assert repository.connection.params == (
        "queried-unit",
        "release-1",
        "queried-unit",
        "queried-unit",
        128,
    )
    assert "r.target_unit_id = %s" in repository.connection.sql
    assert relation.source_unit_id == "amending-unit"
    assert relation.target_unit_id == "queried-unit"
    assert neighbor.unit_id == "amending-unit"


def test_active_release_is_an_immutable_idempotent_noop() -> None:
    repository = _ExistingReleaseRepository()

    writable = repository.stage_release(
        release_id="legal-index-fixed",
        source_release_id="mysql-release-1",
        source_manifest_sha256="a" * 64,
        projection_version="legal-evidence-projection-v1",
        embedding_profile_id=None,
    )

    assert writable is False
    assert not any("DO UPDATE" in sql for sql in repository.connection.statements)


def test_release_id_changes_with_publication_inputs_and_existing_release_skips_writes() -> (
    None
):
    first_target = _NoopPublishedTarget()
    result = LegalEvidenceIndexer(_NoopSource(), first_target).publish()

    assert result["idempotent_existing_release"] is True
    assert result["projected_units"] == 0
    assert first_target.release_id.startswith("legal-index-")
    assert len(first_target.release_id) == len("legal-index-") + 32


def test_embedding_adapter_uses_model_gateway_component_header(monkeypatch) -> None:
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.25, 0.75]}]}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return _Response()

    monkeypatch.setattr("contract.legal_evidence.embedding.httpx.post", fake_post)
    provider = OpenAICompatibleLegalEmbeddingProvider(
        base_url="http://model-gateway/v1",
        api_key="test-key",
        registration_id="embedding-component",
        model="embedding-model",
        dimensions=2,
    )

    assert provider.embed_query("合同") == [0.25, 0.75]
    assert captured["headers"]["X-Aituge-Model-Component-ID"] == "embedding-component"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert "X-Model-Registration-Id" not in captured["headers"]


def test_embedding_adapter_omits_authorization_header_when_token_is_empty(
    monkeypatch,
) -> None:
    captured = {}

    class _Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": [{"index": 0, "embedding": [0.25, 0.75]}]}

    def fake_post(_url, *, headers, json, timeout):
        del json, timeout
        captured["headers"] = headers
        return _Response()

    monkeypatch.setattr("contract.legal_evidence.embedding.httpx.post", fake_post)
    provider = OpenAICompatibleLegalEmbeddingProvider(
        base_url="http://model-gateway/v1",
        api_key="",
        registration_id="embedding-component",
        model="embedding-model",
        dimensions=2,
    )

    assert provider.embed_query("合同") == [0.25, 0.75]
    assert "Authorization" not in captured["headers"]


def test_embedding_adapter_retries_transient_gateway_failure(monkeypatch) -> None:
    attempts = []
    sleeps = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.25, 0.75]}]}

    def fake_post(url, *, headers, json, timeout):
        del headers, json, timeout
        attempts.append(url)
        if len(attempts) == 1:
            response = httpx.Response(503, request=httpx.Request("POST", url))
            raise httpx.HTTPStatusError(
                "temporary gateway failure",
                request=response.request,
                response=response,
            )
        return _Response()

    monkeypatch.setattr("contract.legal_evidence.embedding.httpx.post", fake_post)
    monkeypatch.setattr("contract.legal_evidence.embedding.time.sleep", sleeps.append)
    provider = OpenAICompatibleLegalEmbeddingProvider(
        base_url="http://model-gateway/v1",
        api_key="test-key",
        registration_id="embedding-component",
        model="embedding-model",
        dimensions=2,
        maximum_attempts=2,
        retry_base_seconds=0.25,
    )

    assert provider.embed_query("合同") == [0.25, 0.75]
    assert len(attempts) == 2
    assert sleeps == [0.25]


def test_indexer_embeds_small_batches_concurrently_and_preserves_order() -> None:
    class _ConcurrentProvider:
        profile_id = "profile"
        provider = "test"
        model = "test"

        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.lock = threading.Lock()

        def embed_documents(self, texts):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return [[float(text.rsplit("-", 1)[-1])] for text in texts]

    provider = _ConcurrentProvider()
    units = [
        LegalRetrievalUnit(
            unit_id=f"unit-{index}",
            release_id="release-1",
            instrument_id="instrument-1",
            version_id="version-1",
            source_node_ids=[f"node-{index}"],
            title="测试法规",
            content=f"content-{index}",
            content_hash=f"{index:064x}",
            sequence=index,
        )
        for index in range(16)
    ]
    indexer = LegalEvidenceIndexer(
        _NoopSource(),
        _NoopPublishedTarget(),
        embedding_provider=provider,
        embedding_batch_size=2,
        embedding_workers=4,
    )

    batches = indexer._embed_batches(units)

    assert provider.maximum_active > 1
    assert [item.unit_id for batch, _ in batches for item in batch] == [
        item.unit_id for item in units
    ]
    assert [vector[0] for _, vectors in batches for vector in vectors] == [
        float(index) for index in range(16)
    ]


def test_indexer_reuses_completed_rows_and_persists_one_embedding_write_per_flush() -> (
    None
):
    class _Source:
        def active_release(self):
            return {"release_id": "source-1", "manifest_sha256": "a" * 64}

        def retrieval_root_count(self, _release_id):
            return 16

        def iter_nodes(self, _release_id, *, page_size):
            del page_size
            for index in range(16):
                yield {
                    "release_id": "source-1",
                    "instrument_key": f"instrument-{index}",
                    "version_id": f"version-{index:02d}",
                    "title": "测试法规",
                    "category_root": "法律",
                    "jurisdiction_code": "CN",
                    "issuing_authority_names_json": "[]",
                    "source_url": None,
                    "node_id": f"node-{index}",
                    "parent_node_id": None,
                    "node_type": "PREAMBLE",
                    "node_number": None,
                    "heading": None,
                    "content_plain": f"content-{index}",
                    "sequence": index,
                    "path": "前言",
                    "content_sha256": f"{index:064x}",
                }

        def iter_instruments(self, _release_id, *, page_size):
            del page_size
            return iter(())

        def iter_relations(self, _release_id, *, page_size):
            del page_size
            return iter(())

    class _Provider:
        profile_id = "profile"
        provider = "test"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            return [[float(index)] for index, _text in enumerate(texts)]

    class _Target:
        def __init__(self) -> None:
            self.embedding_writes = []
            self.relations_reset = False

        def stage_release(self, **_kwargs):
            return True

        @staticmethod
        def units_requiring_projection(units):
            return list(units)[8:]

        @staticmethod
        def units_requiring_embedding(units, *, profile_id):
            assert profile_id == "profile"
            return list(units)[4:]

        @staticmethod
        def upsert_units(units):
            assert len(list(units)) == 8
            return 8

        def upsert_embeddings(self, *, units, vectors, **_kwargs):
            self.embedding_writes.append((list(units), list(vectors)))
            return len(units)

        def reset_staged_relations(self, _release_id):
            self.relations_reset = True

        @staticmethod
        def representative_unit_ids(_release_id):
            return {}

        @staticmethod
        def iter_units(_release_id, *, page_size):
            del page_size
            return iter(())

        @staticmethod
        def unit_ids_by_instrument_articles(_release_id, _keys):
            return {}

        @staticmethod
        def representative_unit_id(*_args):
            return None

        @staticmethod
        def upsert_relations(relations):
            return len(list(relations))

        @staticmethod
        def mark_projection_complete(_release_id, *, expected_unit_count):
            assert expected_unit_count == 16
            return 16, 0

    provider = _Provider()
    target = _Target()
    result = LegalEvidenceIndexer(
        _Source(),
        target,
        embedding_provider=provider,
        write_batch_size=16,
        embedding_batch_size=2,
        embedding_workers=1,
    ).publish()

    assert provider.calls == 6
    assert len(target.embedding_writes) == 1
    assert len(target.embedding_writes[0][0]) == 12
    assert len(target.embedding_writes[0][1]) == 12
    assert result["projected_units"] == 16
    assert result["new_units"] == 8
    assert result["embedded_units"] == 16
    assert result["new_embeddings"] == 12
    assert result["reused_units"] == 8
    assert result["reused_embeddings"] == 4
    assert target.relations_reset is True


def test_indexer_resumes_from_last_durable_version_and_checkpoints_progress() -> None:
    existing = LegalRetrievalUnit(
        unit_id="legal-unit-existing",
        release_id="projection-1",
        instrument_id="instrument-1",
        version_id="version-01",
        source_node_ids=["node-1"],
        title="测试法规一",
        content="既有投影。",
        content_hash="1" * 64,
        sequence=1,
    )

    class _Source:
        start_version_id = None

        @staticmethod
        def active_release():
            return {"release_id": "source-1", "manifest_sha256": "a" * 64}

        @staticmethod
        def retrieval_root_count(_release_id):
            return 2

        def iter_nodes(self, _release_id, *, page_size, start_version_id=""):
            del page_size
            self.start_version_id = start_version_id
            for index in (1, 2):
                yield {
                    "release_id": "source-1",
                    "instrument_key": f"instrument-{index}",
                    "version_id": f"version-{index:02d}",
                    "title": f"测试法规{index}",
                    "category_root": "法律",
                    "jurisdiction_code": "CN",
                    "issuing_authority_names_json": "[]",
                    "source_url": None,
                    "node_id": f"node-{index}",
                    "parent_node_id": None,
                    "node_type": "PREAMBLE",
                    "node_number": None,
                    "heading": None,
                    "content_plain": "既有投影。" if index == 1 else "新增投影。",
                    "sequence": 1,
                    "path": "前言",
                    "content_sha256": str(index) * 64,
                }

        @staticmethod
        def iter_instruments(_release_id, *, page_size):
            del page_size
            return iter(())

        @staticmethod
        def iter_relations(_release_id, *, page_size):
            del page_size
            return iter(())

    class _Target:
        def __init__(self) -> None:
            self.units = {existing.unit_id: existing}
            self.cursor = ("version-01", 1, 1, 0)
            self.checkpoints = []

        @staticmethod
        def stage_release(**_kwargs):
            return True

        def projection_resume_cursor(self, _release_id):
            return self.cursor

        def units_requiring_projection(self, units):
            existing_versions = {unit.version_id for unit in self.units.values()}
            return [unit for unit in units if unit.version_id not in existing_versions]

        def upsert_units(self, units):
            units = list(units)
            self.units.update({unit.unit_id: unit for unit in units})
            return len(units)

        def record_projection_checkpoint(self, **checkpoint):
            self.checkpoints.append(checkpoint)
            self.cursor = (
                checkpoint["last_version_id"],
                checkpoint["last_sequence"],
                len(self.units),
                0,
            )

        @staticmethod
        def reset_staged_relations(_release_id):
            return None

        @staticmethod
        def representative_unit_ids(_release_id):
            return {}

        def iter_units(self, _release_id, *, page_size):
            del page_size
            return iter(sorted(self.units.values(), key=lambda unit: unit.version_id))

        @staticmethod
        def unit_ids_by_instrument_articles(_release_id, _keys):
            return {}

        @staticmethod
        def representative_unit_id(*_args):
            return None

        @staticmethod
        def upsert_relations(relations):
            return len(list(relations))

        def mark_projection_complete(self, _release_id, *, expected_unit_count):
            assert expected_unit_count == len(self.units) == 2
            return 2, 0

        @staticmethod
        def projection_counts(_release_id):
            return 2, 0, 0

    source = _Source()
    target = _Target()
    result = LegalEvidenceIndexer(source, target, write_batch_size=2).publish()

    assert source.start_version_id == "version-01"
    assert result["actual_units"] == 2
    assert result["new_units"] == 1
    assert result["reused_units"] == 1
    assert target.checkpoints[-1]["status"] == "SUCCEEDED"
    assert target.checkpoints[-1]["projected_unit_count"] == 2


def test_embedding_batch_failure_writes_no_partial_embedding_flush() -> None:
    class _Source:
        def active_release(self):
            return {"release_id": "source-1", "manifest_sha256": "a" * 64}

        def retrieval_root_count(self, _release_id):
            return 4

        def iter_nodes(self, _release_id, *, page_size):
            del page_size
            for index in range(4):
                yield {
                    "release_id": "source-1",
                    "instrument_key": f"instrument-{index}",
                    "version_id": f"version-{index}",
                    "title": "测试法规",
                    "category_root": "法律",
                    "jurisdiction_code": "CN",
                    "issuing_authority_names_json": "[]",
                    "source_url": None,
                    "node_id": f"node-{index}",
                    "parent_node_id": None,
                    "node_type": "PREAMBLE",
                    "node_number": None,
                    "heading": None,
                    "content_plain": f"content-{index}",
                    "sequence": index,
                    "path": "前言",
                    "content_sha256": f"{index:064x}",
                }

    class _Provider:
        profile_id = "profile"
        provider = "test"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("embedding batch failed")
            return [[0.0] for _text in texts]

    class _Target:
        def __init__(self) -> None:
            self.embedding_writes = 0

        @staticmethod
        def stage_release(**_kwargs):
            return True

        @staticmethod
        def upsert_units(units):
            return len(list(units))

        def upsert_embeddings(self, **_kwargs):
            self.embedding_writes += 1
            return 0

    target = _Target()
    with pytest.raises(RuntimeError, match="embedding batch failed"):
        LegalEvidenceIndexer(
            _Source(),
            target,
            embedding_provider=_Provider(),
            write_batch_size=4,
            embedding_batch_size=2,
            embedding_workers=1,
        ).publish()

    assert target.embedding_writes == 0
