from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx
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
    assert "X-Model-Registration-Id" not in captured["headers"]


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
