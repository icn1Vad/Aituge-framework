from __future__ import annotations

import copy
import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proof.application.structure import extract_policy_structure
from proof.domain.policy_grouping import POLICY_GROUP_NAMES, infer_policy_category
from proof.errors import ProofError
from proof.infrastructure.parsers import parse_document


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DatasetGroup:
    code: str
    name: str
    relative_directory: str
    level_code: str
    extensions: frozenset[str]


DATASET_GROUPS = (
    DatasetGroup("original", "原始制度", ".", "peer", frozenset({".pdf"})),
    DatasetGroup(
        "company_revised",
        "修改版公司制度",
        "output/01_修改后公司规则文档",
        "peer",
        frozenset({".docx"}),
    ),
    DatasetGroup(
        "subsidiary_l2",
        "二级公司制度",
        "output/02_二级公司局部制度",
        "lower",
        frozenset({".docx"}),
    ),
    DatasetGroup(
        "subsidiary_l3",
        "三级公司制度",
        "output/03_三级子公司局部制度",
        "lower",
        frozenset({".docx"}),
    ),
)

CATEGORY_NAMES = POLICY_GROUP_NAMES
LEVEL_NAMES = {
    "upper": "上级制度",
    "peer": "本级/同级制度",
    "lower": "下级制度",
}

class DatasetAuditor:
    def __init__(self, root: Path, *, max_upload_bytes: int, long_clause_chars: int = 3000) -> None:
        self.root = root.expanduser().resolve()
        self.max_upload_bytes = max_upload_bytes
        self.long_clause_chars = long_clause_chars
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._paths: dict[str, Path] = {}

    def audit(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if refresh or self._snapshot is None:
                self._snapshot = self._scan()
            return copy.deepcopy(self._snapshot)

    def file_detail(self, file_id: str) -> dict[str, Any]:
        self.audit()
        path = self._paths.get(file_id)
        if path is None:
            raise ProofError("dataset_file_not_found", "Dataset file not found.", status_code=404)
        group = self._group_for_path(path)
        return self._scan_file(path, group, include_text=True)

    def path_for_file(self, file_id: str) -> Path:
        self.audit()
        path = self._paths.get(file_id)
        if path is None:
            raise ProofError("dataset_file_not_found", "Dataset file not found.", status_code=404)
        return path

    def _scan(self) -> dict[str, Any]:
        if not self.root.is_dir():
            raise ProofError(
                "dataset_root_not_found",
                "Configured dataset directory does not exist.",
                status_code=503,
            )
        self._paths = {}
        files: list[dict[str, Any]] = []
        for group in DATASET_GROUPS:
            directory = (self.root / group.relative_directory).resolve()
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if not path.is_file() or path.suffix.lower() not in group.extensions:
                    continue
                record = self._scan_file(path, group, include_text=False)
                files.append(record)
                self._paths[record["id"]] = path

        group_counts = []
        for group in DATASET_GROUPS:
            matching = [item for item in files if item["group_code"] == group.code]
            group_counts.append(
                {
                    "code": group.code,
                    "name": group.name,
                    "file_count": len(matching),
                    "ready_count": sum(item["scan_status"] == "ready" for item in matching),
                    "clause_count": sum(item["clause_count"] for item in matching),
                    "mixed_structure_count": sum(item.get("structure_profile") == "mixed" for item in matching),
                }
            )
        category_counts = [
            {
                "code": code,
                "name": name,
                "file_count": sum(item["category_code"] == code for item in files),
            }
            for code, name in CATEGORY_NAMES.items()
        ]
        return {
            "root_name": self.root.name,
            "summary": {
                "candidate_file_count": len(files),
                "ready_file_count": sum(item["scan_status"] == "ready" for item in files),
                "blocked_file_count": sum(item["scan_status"] == "blocked" for item in files),
                "anomaly_file_count": sum(bool(item["anomalies"]) for item in files),
                "block_count": sum(item["block_count"] for item in files),
                "clause_count": sum(item["clause_count"] for item in files),
                "mixed_structure_count": sum(item.get("structure_profile") == "mixed" for item in files),
            },
            "groups": group_counts,
            "categories": category_counts,
            "files": files,
        }

    def _scan_file(self, path: Path, group: DatasetGroup, *, include_text: bool) -> dict[str, Any]:
        relative_path = path.relative_to(self.root).as_posix()
        content = path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        title = _infer_title(path)
        category_code = infer_policy_category(title)
        record: dict[str, Any] = {
            "id": hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24],
            "relative_path": relative_path,
            "original_name": path.name,
            "title": title,
            "group_code": group.code,
            "group_name": group.name,
            "level_code": group.level_code,
            "level_name": LEVEL_NAMES[group.level_code],
            "category_code": category_code,
            "category_name": CATEGORY_NAMES[category_code],
            "file_type": path.suffix.lower().lstrip("."),
            "file_size": len(content),
            "content_hash": content_hash,
            "scan_status": "ready",
            "block_count": 0,
            "clause_count": 0,
            "warning_count": 0,
            "shortest_clause_chars": None,
            "longest_clause_chars": None,
            "structure_profile": None,
            "structure_profile_name": "未识别",
            "profiles_detected": [],
            "structure_regions": [],
            "structure_coverage_rate": 0.0,
            "anomalies": [],
        }
        if len(content) > self.max_upload_bytes:
            record["scan_status"] = "blocked"
            record["error_code"] = "file_too_large"
            record["error_message"] = "File exceeds the configured upload limit."
            record["anomalies"].append(
                _anomaly("file_too_large", "blocking", "文件超过当前上传大小限制。")
            )
            return record

        try:
            parsed = parse_document(path)
            record["block_count"] = len(parsed.blocks)
            record["warning_count"] = len(parsed.warnings)
            extraction = extract_policy_structure(parsed.blocks)
            units = extraction.units
        except ProofError as exc:
            record["scan_status"] = "blocked"
            record["error_code"] = exc.code
            record["error_message"] = str(exc)
            record["anomalies"].append(_anomaly(exc.code, "blocking", _error_message(exc.code)))
            return record
        except Exception:
            logger.exception("Unexpected dataset scan failure for %s", path)
            record["scan_status"] = "blocked"
            record["error_code"] = "dataset_scan_failed"
            record["error_message"] = "Dataset file scan failed."
            record["anomalies"].append(
                _anomaly("dataset_scan_failed", "blocking", "文件扫描失败，请查看程序日志。")
            )
            return record

        lengths = [len(unit.text) for unit in units]
        record["structure_profile"] = extraction.profile
        record["structure_profile_name"] = _profile_name(extraction.profile)
        record["profiles_detected"] = extraction.profiles_detected
        record["structure_regions"] = extraction.regions
        record["structure_coverage_rate"] = extraction.diagnostics["content_coverage_rate"]
        record["structure_diagnostics"] = extraction.diagnostics
        record["clause_count"] = len(units)
        record["shortest_clause_chars"] = min(lengths)
        record["longest_clause_chars"] = max(lengths)
        if parsed.warnings:
            record["anomalies"].append(
                _anomaly("parser_warning", "warning", f"解析器产生 {len(parsed.warnings)} 条 warning。")
            )
        record["anomalies"].extend(
            _anomaly(item["code"], item.get("severity", "warning"), item["message"])
            for item in extraction.warnings
        )
        long_count = sum(length > self.long_clause_chars for length in lengths)
        if long_count:
            record["anomalies"].append(
                _anomaly(
                    "long_clause",
                    "review",
                    f"{long_count} 个条款超过 {self.long_clause_chars} 字，最长 {max(lengths)} 字；仍保持完整条款。",
                )
            )
        short_count = sum(length < 20 for length in lengths)
        if short_count:
            record["anomalies"].append(
                _anomaly("short_clause", "review", f"{short_count} 个条款少于 20 字，建议人工抽查。")
            )
        if include_text:
            record["clauses"] = [
                {
                    "id": unit.id,
                    "clause_no_raw": unit.clause_no_raw,
                    "clause_ordinal": unit.clause_ordinal,
                    "unit_type": unit.unit_type,
                    "text": unit.text,
                    "text_chars": len(unit.text),
                    "heading_path": unit.heading_path,
                    "source_block_ids": unit.source_block_ids,
                    "page_start": unit.page_start,
                    "page_end": unit.page_end,
                    "paragraph_start": unit.paragraph_start,
                    "paragraph_end": unit.paragraph_end,
                    "char_start": unit.char_start,
                    "char_end": unit.char_end,
                    "text_hash": unit.text_hash,
                }
                for unit in units
            ]
            record["parse_warnings"] = parsed.warnings
        return record

    def _group_for_path(self, path: Path) -> DatasetGroup:
        for group in DATASET_GROUPS:
            directory = (self.root / group.relative_directory).resolve()
            if path.parent == directory and path.suffix.lower() in group.extensions:
                return group
        raise ProofError("dataset_file_not_found", "Dataset file not found.", status_code=404)


def _infer_title(path: Path) -> str:
    stem = path.stem
    if "《" in stem and "》" in stem:
        return stem.split("《", 1)[1].split("》", 1)[0].strip()
    if "[" in stem and "]" in stem:
        return stem.split("[", 1)[1].split("]", 1)[0].strip()
    for marker in ("_二级公司", "_三级子公司"):
        if marker in stem:
            prefix = stem.split(marker, 1)[0]
            return prefix.split("_", 2)[-1].strip()
    return stem


def _anomaly(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _error_message(code: str) -> str:
    if code == "no_clauses_found":
        return "未找到可形成稳定序列的预制编号结构，当前不入库。"
    if code == "ocr_required":
        return "PDF 没有可选文本，需要 OCR 后才能入库。"
    return "文件无法通过当前入库前检查。"


def _profile_name(profile: str) -> str:
    return {
        "article": "第X条",
        "decimal_outline": "小数层级",
        "chinese_outline": "中文序号",
        "mixed": "混合结构",
    }.get(profile, profile)
