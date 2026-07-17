from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proof.application.dataset_audit import DatasetAuditor
from proof.application.structure import STRUCTURE_ENGINE_VERSION, extract_policy_structure
from proof.config import Settings
from proof.domain import DocumentBlock, StructureExtractionResult
from proof.domain.numbering import MarkerKind, detect_numbering_marker, is_toc_entry
from proof.infrastructure.parsers import parse_document
from proof.infrastructure.postgres.repository import ProofRepository


MIN_STRUCTURE_COVERAGE = 0.95
_BOUNDARY_KINDS = {
    "article": {MarkerKind.ARTICLE},
    "decimal_outline": {
        MarkerKind.DECIMAL,
        MarkerKind.ARABIC_HEADING,
        MarkerKind.ARABIC_ITEM,
    },
    "chinese_outline": {
        MarkerKind.CHINESE_HEADING,
        MarkerKind.PAREN_CHINESE,
    },
}


class DatasetStructureValidator:
    def __init__(self, settings: Settings, root: Path) -> None:
        self.settings = settings
        self.root = root.expanduser().resolve()
        self.auditor = DatasetAuditor(self.root, max_upload_bytes=settings.max_upload_bytes)
        self.repository = ProofRepository(settings)

    def validate(self) -> dict[str, Any]:
        snapshot = self.auditor.audit(refresh=True)
        documents = self.repository.get_documents_by_content_hashes(
            [item["content_hash"] for item in snapshot["files"]]
        )
        units_by_document = self.repository.get_structure_units_by_document_ids(
            [item["document_id"] for item in documents.values()]
        )
        files: list[dict[str, Any]] = []
        profile_counts: Counter[str] = Counter()
        total_units = 0

        for item in snapshot["files"]:
            result = self._validate_file(item, documents.get(item["content_hash"]), units_by_document)
            files.append(result)
            if result["structure_profile"]:
                profile_counts[result["structure_profile"]] += 1
            total_units += result["unit_count"]

        invalid_files = [item for item in files if not item["chunk_validation_ok"]]
        review_files = [item for item in files if item["chunk_validation_ok"] and item["review_findings"]]
        dataset_chunk_digest = hashlib.sha256(
            "\n".join(
                f"{item['relative_path']}|{item['content_hash']}|{item['structure_profile']}|{item['chunk_digest']}"
                for item in sorted(files, key=lambda value: value["relative_path"])
            ).encode("utf-8")
        ).hexdigest()
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "dataset_root": str(self.root),
            "structure_engine_version": STRUCTURE_ENGINE_VERSION,
            "thresholds": {"minimum_structure_coverage": MIN_STRUCTURE_COVERAGE},
            "summary": {
                "candidate_file_count": len(files),
                "validated_file_count": len(files) - len(invalid_files),
                "invalid_file_count": len(invalid_files),
                "clean_file_count": len(files) - len(invalid_files) - len(review_files),
                "review_file_count": len(review_files),
                "database_match_count": sum(item["database_match"] for item in files),
                "unit_count": total_units,
                "profile_counts": dict(sorted(profile_counts.items())),
                "minimum_structure_coverage": min(
                    (item["structure_coverage_rate"] for item in files),
                    default=0.0,
                ),
                "dataset_chunk_digest": dataset_chunk_digest,
            },
            "checks": {
                "all_candidates_parsed": all(item["parse_ok"] for item in files),
                "all_chunk_invariants_passed": not invalid_files,
                "all_candidates_ingested": all(item["database_document_found"] for item in files),
                "all_database_chunks_match": all(item["database_match"] for item in files),
                "all_database_documents_on_current_engine": all(
                    item["database_engine_current"] for item in files
                ),
            },
            "files": files,
        }

    def _validate_file(
        self,
        item: dict[str, Any],
        document: dict[str, Any] | None,
        units_by_document: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        extraction: StructureExtractionResult | None = None
        try:
            parsed = parse_document(self.auditor.path_for_file(item["id"]))
            extraction = extract_policy_structure(parsed.blocks)
            errors.extend(validate_structure_extraction(parsed.blocks, extraction))
        except Exception as exc:
            parsed = None
            errors.append(
                _finding(
                    "structure_validation_failed",
                    "critical",
                    "文件无法完成解析和结构验收。",
                    {"error_type": type(exc).__name__, "error_message": str(exc)},
                )
            )

        database_found = document is not None
        database_match = False
        database_engine_current = False
        if extraction is not None and document is not None:
            stored_units = units_by_document.get(document["document_id"], [])
            expected = [_unit_signature(unit) for unit in extraction.units]
            actual = [_stored_unit_signature(unit) for unit in stored_units]
            database_match = (
                document.get("structure_profile") == extraction.profile
                and expected == actual
            )
            database_engine_current = (
                document.get("chunker_version") == STRUCTURE_ENGINE_VERSION
                and document.get("structure_diagnostics", {}).get("engine_version")
                == STRUCTURE_ENGINE_VERSION
            )
            if not database_match:
                errors.append(
                    _finding(
                        "database_chunk_mismatch",
                        "critical",
                        "重新解析得到的 chunks 与 PostgreSQL 已入库结果不一致。",
                        {
                            "expected_unit_count": len(expected),
                            "stored_unit_count": len(actual),
                            "expected_profile": extraction.profile,
                            "stored_profile": document.get("structure_profile"),
                        },
                    )
                )
            if not database_engine_current:
                errors.append(
                    _finding(
                        "database_engine_outdated",
                        "high",
                        "数据库文档尚未由当前结构引擎生成完整诊断。",
                        {
                            "stored_chunker_version": document.get("chunker_version"),
                            "expected_chunker_version": STRUCTURE_ENGINE_VERSION,
                        },
                    )
                )
        elif extraction is not None:
            errors.append(
                _finding(
                    "dataset_file_not_ingested",
                    "critical",
                    "数据集文件没有对应的 PostgreSQL document。",
                )
            )

        review_findings = list(item.get("anomalies") or [])
        return {
            "id": item["id"],
            "relative_path": item["relative_path"],
            "content_hash": item["content_hash"],
            "parse_ok": parsed is not None,
            "chunk_validation_ok": not errors,
            "structure_profile": extraction.profile if extraction else item.get("structure_profile"),
            "profiles_detected": extraction.profiles_detected if extraction else [],
            "unit_count": len(extraction.units) if extraction else 0,
            "chunk_digest": _extraction_digest(extraction) if extraction else "",
            "structure_coverage_rate": (
                extraction.diagnostics["content_coverage_rate"] if extraction else 0.0
            ),
            "database_document_found": database_found,
            "database_match": database_match,
            "database_engine_current": database_engine_current,
            "validation_errors": errors,
            "review_findings": review_findings,
        }


def validate_structure_extraction(
    blocks: list[DocumentBlock],
    extraction: StructureExtractionResult,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    block_by_id = {block.id: block for block in blocks}
    block_position = {block.id: index for index, block in enumerate(blocks)}
    source_ids = [block_id for unit in extraction.units for block_id in unit.source_block_ids]

    if not extraction.units:
        errors.append(_finding("empty_extraction", "critical", "结构识别没有生成任何 unit。"))
        return errors
    if [unit.clause_ordinal for unit in extraction.units] != list(range(1, len(extraction.units) + 1)):
        errors.append(_finding("invalid_unit_ordinals", "high", "unit ordinal 不连续。"))
    if len(source_ids) != len(set(source_ids)):
        errors.append(_finding("overlapping_source_blocks", "critical", "同一 source block 被多个 unit 重复使用。"))
    missing_ids = [block_id for block_id in source_ids if block_id not in block_by_id]
    if missing_ids:
        errors.append(
            _finding(
                "missing_source_blocks",
                "critical",
                "unit 引用了不存在的 source block。",
                {"count": len(missing_ids)},
            )
        )

    previous_start = -1
    for unit in extraction.units:
        if not unit.text.strip() or not unit.source_block_ids:
            errors.append(
                _finding(
                    "empty_unit",
                    "critical",
                    "发现正文或 source blocks 为空的 unit。",
                    {"clause_ordinal": unit.clause_ordinal},
                )
            )
            continue
        first_block = block_by_id.get(unit.source_block_ids[0])
        if first_block is None:
            continue
        positions = [block_position[block_id] for block_id in unit.source_block_ids if block_id in block_position]
        if positions != sorted(positions):
            errors.append(
                _finding(
                    "source_block_order_invalid",
                    "high",
                    "unit 内 source blocks 没有保持原文顺序。",
                    {"clause_ordinal": unit.clause_ordinal},
                )
            )
        if positions and positions[0] <= previous_start:
            errors.append(
                _finding(
                    "unit_order_invalid",
                    "high",
                    "units 没有保持原文顺序。",
                    {"clause_ordinal": unit.clause_ordinal},
                )
            )
        if positions:
            previous_start = positions[0]
        marker = detect_numbering_marker(first_block.text)
        allowed = _BOUNDARY_KINDS.get(unit.unit_type, set())
        if marker is None or marker.kind not in allowed:
            errors.append(
                _finding(
                    "invalid_unit_boundary",
                    "critical",
                    "unit 没有从其模板允许的编号边界开始。",
                    {
                        "clause_ordinal": unit.clause_ordinal,
                        "unit_type": unit.unit_type,
                        "first_text": first_block.text[:120],
                    },
                )
            )
        elif marker.raw != unit.clause_no_raw:
            errors.append(
                _finding(
                    "boundary_label_mismatch",
                    "high",
                    "unit 标签与原文起始编号不一致。",
                    {
                        "clause_ordinal": unit.clause_ordinal,
                        "unit_label": unit.clause_no_raw,
                        "source_label": marker.raw,
                    },
                )
            )
        expected_hash = hashlib.sha256(unit.text.encode("utf-8")).hexdigest()
        if unit.text_hash != expected_hash:
            errors.append(
                _finding(
                    "unit_hash_mismatch",
                    "critical",
                    "unit text_hash 与正文不一致。",
                    {"clause_ordinal": unit.clause_ordinal},
                )
            )

    article_units = sum(unit.unit_type == "article" for unit in extraction.units)
    article_markers = 0
    for region in extraction.regions:
        if region["profile"] != "article":
            continue
        for block in blocks:
            if not (
                region["block_start_ordinal"] <= block.ordinal <= region["block_end_ordinal"]
            ) or is_toc_entry(block.text):
                continue
            marker = detect_numbering_marker(block.text)
            article_markers += bool(marker and marker.kind == MarkerKind.ARTICLE)
    if article_units != article_markers:
        errors.append(
            _finding(
                "article_boundary_count_mismatch",
                "critical",
                "article 区域中的“第X条”数量与 article units 数量不一致。",
                {"article_markers": article_markers, "article_units": article_units},
            )
        )
    coverage = extraction.diagnostics.get("content_coverage_rate", 0.0)
    if coverage < MIN_STRUCTURE_COVERAGE:
        errors.append(
            _finding(
                "structure_coverage_too_low",
                "high",
                "结构区域正文覆盖率低于验收阈值。",
                {"coverage": coverage, "minimum": MIN_STRUCTURE_COVERAGE},
            )
        )
    if extraction.profile == "mixed" and not any(
        warning.get("code") == "mixed_numbering_profiles" for warning in extraction.warnings
    ):
        errors.append(_finding("mixed_warning_missing", "high", "混合结构没有生成显式诊断。"))
    return errors


def _unit_signature(unit) -> tuple[Any, ...]:
    return (
        unit.clause_ordinal,
        unit.clause_no_raw,
        unit.unit_type,
        unit.text_hash,
        tuple(unit.heading_path),
        unit.page_start,
        unit.page_end,
        unit.paragraph_start,
        unit.paragraph_end,
    )


def _stored_unit_signature(unit: dict[str, Any]) -> tuple[Any, ...]:
    return (
        unit["clause_ordinal"],
        unit["clause_no_raw"],
        unit["unit_type"],
        unit["text_hash"],
        tuple(unit.get("heading_path") or []),
        unit.get("page_start"),
        unit.get("page_end"),
        unit.get("paragraph_start"),
        unit.get("paragraph_end"),
    )


def _extraction_digest(extraction: StructureExtractionResult) -> str:
    payload = [
        {
            "ordinal": unit.clause_ordinal,
            "label": unit.clause_no_raw,
            "type": unit.unit_type,
            "text_hash": unit.text_hash,
            "heading_path": unit.heading_path,
            "page_start": unit.page_start,
            "page_end": unit.page_end,
        }
        for unit in extraction.units
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finding(
    code: str,
    severity: str,
    message: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": evidence or {},
    }
