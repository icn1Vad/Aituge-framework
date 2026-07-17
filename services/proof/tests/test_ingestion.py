from __future__ import annotations

from dataclasses import asdict

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
