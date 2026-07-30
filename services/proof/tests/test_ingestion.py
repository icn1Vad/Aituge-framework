from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from proof.application.chunking import split_into_clause_units
from proof.application.ingestion import (
    NativePolicySourceParser,
    PolicyIngestionPipeline,
    PresetPolicyStructureExtractor,
)
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.parsers import parse_document_bytes


def test_default_parser_adapter_matches_existing_parser() -> None:
    content = """示例制度
第一章 总则
第一条 第一款内容。
补充段落。
""".encode()

    expected = parse_document_bytes(content, "policy.txt")
    actual = NativePolicySourceParser().parse(content, "policy.txt")

    assert actual.file_type == expected.file_type
    assert actual.warnings == expected.warnings
    assert [_block_without_id(block) for block in actual.blocks] == [
        _block_without_id(block) for block in expected.blocks
    ]


def test_default_clause_extractor_matches_existing_splitter() -> None:
    parsed = parse_document_bytes(
        """第一章 总则
第一条 第一款。
第二段。
第二条 第二款。
""".encode(),
        "policy.txt",
    )

    expected = split_into_clause_units(parsed.blocks)
    actual = PresetPolicyStructureExtractor().extract(parsed.blocks).units

    assert [_unit_without_id(unit) for unit in actual] == [_unit_without_id(unit) for unit in expected]


def test_unexpected_parser_error_is_sanitized_and_run_is_retained(tmp_path) -> None:
    repository = MemoryRunRepository()
    pipeline = PolicyIngestionPipeline(
        Settings(storage_root=tmp_path),
        repository,
        parser=ExplodingParser(),
    )

    with pytest.raises(ProofError) as exc_info:
        pipeline.ingest_policy(content=b"content", filename="policy.txt")

    assert exc_info.value.code == "ingestion_failed"
    assert str(exc_info.value) == "Policy ingestion failed."
    run_id = exc_info.value.details["ingestion_run_id"]
    assert repository.failed == {
        "run_id": run_id,
        "stage": "parse",
        "error_code": "ingestion_failed",
        "error_message": "Policy ingestion failed.",
        "error_details": {},
    }


def test_ingestion_auto_classifies_policy_from_title(tmp_path) -> None:
    repository = CapturingRepository()
    pipeline = PolicyIngestionPipeline(Settings(storage_root=tmp_path), repository)

    result = pipeline.ingest_policy(
        content="第一条 采购事项由采购部门负责。".encode(),
        filename="采购管理办法.txt",
        title="采购管理办法",
    )

    assert result["reused"] is False
    assert repository.ingested["category_code"] == "procurement_supply"
    assert repository.ingested["normalized_title"] == "采购管理"


def test_ingestion_persists_confirmed_title_and_version(tmp_path) -> None:
    repository = CapturingRepository()
    pipeline = PolicyIngestionPipeline(Settings(storage_root=tmp_path), repository)

    pipeline.ingest_policy(
        content="第一条 数据安全事项由责任部门负责。".encode(),
        filename="01-原始上传名-V2.3.4.txt",
        title="管理员确认的制度名称",
        version="V2.3.4",
    )

    assert repository.ingested["original_name"] == "01-原始上传名-V2.3.4.txt"
    assert repository.ingested["title"] == "管理员确认的制度名称"
    assert repository.ingested["version"] == "v2.3.4"
    assert repository.ingested["version_seq"] == 134


def test_ingestion_rejects_invalid_policy_version_before_writes(tmp_path) -> None:
    repository = NoAccessRepository()
    pipeline = PolicyIngestionPipeline(Settings(storage_root=tmp_path), repository)

    with pytest.raises(ProofError) as exc_info:
        pipeline.ingest_policy(
            content=b"content",
            filename="policy.txt",
            version="1.0",
        )

    assert exc_info.value.code == "invalid_policy_version"
    assert repository.accessed == []


def test_similarity_preview_detects_fixture_change_without_writes(tmp_path) -> None:
    examples = Path(__file__).parents[1] / "examples" / "similarity-test"
    primary = examples / "01-研发项目与数据安全管理办法-v1.0.0.txt"
    candidate = examples / "02-研发项目与数据安全管理办法-约10%修改版.txt"
    repository = SimilarityRepository()
    pipeline = PolicyIngestionPipeline(Settings(storage_root=tmp_path), repository)

    result = pipeline.preview_similarity(
        content=primary.read_bytes(),
        filename=primary.name,
        candidate_files=[(candidate.read_bytes(), candidate.name)],
    )

    assert result["primary_file_name"] == primary.name
    assert result["status"] == "decision_required"
    assert result["candidates"][0]["candidate_file_name"] == candidate.name
    assert result["candidates"][0]["similarity_score"] == 0.9
    assert result["candidates"][0]["estimated_change_percent"] == 11

    reverse = pipeline.preview_similarity(
        content=candidate.read_bytes(),
        filename=candidate.name,
        candidate_files=[(primary.read_bytes(), primary.name)],
    )
    symmetric_fields = {
        "similarity_score",
        "edit_similarity",
        "jaccard",
        "containment",
        "length_ratio",
        "clause_coverage",
        "estimated_change_percent",
    }
    assert {
        key: reverse["candidates"][0][key] for key in symmetric_fields
    } == {
        key: result["candidates"][0][key] for key in symmetric_fields
    }
    assert repository.lookups == [
        "content_hash",
        "similarity_candidates",
        "content_hash",
        "similarity_candidates",
    ]
    assert list(tmp_path.iterdir()) == []


def _block_without_id(block) -> dict:
    value = asdict(block)
    value.pop("id")
    return value


def _unit_without_id(unit) -> dict:
    value = asdict(unit)
    value.pop("id")
    return value


class ExplodingParser:
    version = "exploding-parser-v1"
    supported_extensions = frozenset({".txt"})

    def parse(self, content: bytes, filename: str):
        raise RuntimeError("internal parser detail")


class MemoryRunRepository:
    def __init__(self) -> None:
        self.failed: dict = {}

    def create_ingestion_run(self, **values) -> None:
        self.created = values

    def get_by_content_hash(self, content_hash: str):
        return None

    def update_ingestion_run(self, run_id: str, **values) -> None:
        self.stage = values["stage"]

    def fail_ingestion_run(self, run_id: str, **values) -> None:
        self.failed = {"run_id": run_id, **values}


class CapturingRepository:
    def __init__(self) -> None:
        self.ingested: dict = {}

    def create_ingestion_run(self, **values) -> None:
        self.created = values

    def get_by_content_hash(self, content_hash: str):
        return None

    def update_ingestion_run(self, run_id: str, **values) -> None:
        self.updated = {"run_id": run_id, **values}

    def ingest(self, **values):
        self.ingested = values
        return {
            "policy": {"id": values["policy_id"], "status": "draft"},
            "document": {"id": values["document_id"]},
            "clauses": [],
        }

    def fail_ingestion_run(self, run_id: str, **values) -> None:
        raise AssertionError(f"Unexpected ingestion failure: {run_id}, {values}")


class NoAccessRepository:
    def __init__(self) -> None:
        self.accessed: list[str] = []

    def __getattr__(self, name: str):
        self.accessed.append(name)
        raise AssertionError(f"Similarity preview accessed repository.{name}")


class SimilarityRepository:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def get_by_content_hash(self, content_hash: str):
        self.lookups.append("content_hash")
        return None

    def list_similarity_candidates(self, **values):
        self.lookups.append("similarity_candidates")
        return []
