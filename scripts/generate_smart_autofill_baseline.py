from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader


REPO_ROOT = Path(__file__).resolve().parents[1]
SMART_AUTOFILL_ROOT = Path(r"E:\MyProjects\SmartAutoFill")
SOURCE_CONFIG = SMART_AUTOFILL_ROOT / "backend" / "config" / "form_fields.json"
OUTPUT_DIR = REPO_ROOT / "docs" / "smart_autofill"

BENCHMARK_FILES = [
    Path(r"E:\MyProjects\proofreading\EPC合同  (Executed 31082023).pdf"),
    Path(r"E:\MyProjects\方案\03-2关于航天长征化学工程股份有限公司收购航天氢能有限公司股权的可行性研究报告（公开）.docx"),
]

ITEM_DEFINITIONS = {
    "project": {
        "name": "项目基本信息与项目分类",
        "skill_package": "smart-fill-project-package",
        "primary_skill": "project-basic-extraction",
        "source_groups": {"project_basic", "project_category", "industry_market_analysis"},
    },
    "company": {
        "name": "投资主体、标的公司、股权结构与资产评估",
        "skill_package": "smart-fill-company-package",
        "primary_skill": "target-company-extraction",
        "source_groups": {
            "investor",
            "target_company",
            "target_company_equity",
            "target_company_asset_valuation",
        },
    },
    "financial": {
        "name": "财务预测与财务可行性",
        "skill_package": "smart-fill-financial-package",
        "primary_skill": "financial-forecast-extraction",
        "source_groups": {"financial_feasibility"},
    },
    "risk": {
        "name": "风险与非财务指标",
        "skill_package": "smart-fill-risk-package",
        "primary_skill": "risk-extraction",
        "source_groups": {"non_financial_indicators", "risk"},
    },
    "analysis": {
        "name": "可行性、必要性与竞争性",
        "skill_package": "smart-fill-analysis-package",
        "primary_skill": "feasibility-analysis-extraction",
        "source_groups": {"feasibility_necessity", "competitiveness"},
    },
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(name: str, payload: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document_baseline(path: Path) -> dict:
    baseline: dict = {
        "absolute_path": str(path),
        "file_name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
    }
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        page_text_lengths = [len(page.extract_text() or "") for page in reader.pages]
        baseline["current_reader"] = {
            "reader": "pypdf",
            "page_count": len(reader.pages),
            "extractable_text_chars": sum(page_text_lengths),
            "pages_with_text": sum(length > 0 for length in page_text_lengths),
        }
    elif path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        baseline["current_reader"] = {
            "reader": "docx-xml",
            "paragraph_nodes": len(root.findall(".//w:p", namespace)),
            "table_nodes": len(root.findall(".//w:tbl", namespace)),
            "text_nodes": len(root.findall(".//w:t", namespace)),
        }
    return baseline


def main() -> None:
    config = read_json(SOURCE_CONFIG)
    groups = config.get("groups", [])
    fields = [
        {
            **field,
            "source_group_id": group["group_id"],
            "source_group_name": group["group_name"],
            "value_shape": "rows"
            if field.get("component_type") in {"table", "manual_table"}
            else "scalar",
            "extraction_mode": "manual_only" if field.get("manual_only") else "automatic",
        }
        for group in groups
        for field in group.get("fields", [])
    ]

    schema_payload = {
        "schema_id": "smart-autofill-79-fields-v1",
        "schema_version": 1,
        "status": "frozen_for_baseline",
        "source_of_truth": str(SOURCE_CONFIG),
        "group_count": len(groups),
        "field_count": len(fields),
        "groups": groups,
        "fields": fields,
    }
    write_json("smart_fill_field_schema.json", schema_payload)

    assigned: dict[str, str] = {}
    items = []
    for item_id, definition in ITEM_DEFINITIONS.items():
        item_fields = [
            field for field in fields if field["source_group_id"] in definition["source_groups"]
        ]
        for field in item_fields:
            if field["field_id"] in assigned:
                raise ValueError(f"Field assigned twice: {field['field_id']}")
            assigned[field["field_id"]] = item_id
        items.append(
            {
                "item_id": item_id,
                "item_name": definition["name"],
                "skill_package": definition["skill_package"],
                "primary_skill": definition["primary_skill"],
                "shared_auxiliary_skills": [
                    "smart-fill-common",
                    "source-citation",
                    "structured-output",
                ],
                "source_group_ids": sorted(definition["source_groups"]),
                "field_count": len(item_fields),
                "automatic_field_ids": [
                    field["field_id"]
                    for field in item_fields
                    if field["extraction_mode"] == "automatic"
                ],
                "manual_only_field_ids": [
                    field["field_id"]
                    for field in item_fields
                    if field["extraction_mode"] == "manual_only"
                ],
                "allowed_field_ids": [field["field_id"] for field in item_fields],
            }
        )

    missing = sorted(set(field["field_id"] for field in fields) - set(assigned))
    if missing:
        raise ValueError(f"Fields missing from item mapping: {missing}")
    mapping_payload = {
        "mapping_id": "smart-autofill-five-parallel-items-v1",
        "schema_id": schema_payload["schema_id"],
        "handler": "batch_item_scheduler",
        "agent_profile": "default-single-agent",
        "max_concurrency": 5,
        "field_count": len(fields),
        "items": items,
        "coverage": {
            "assigned_field_count": len(assigned),
            "missing_field_ids": [],
            "duplicate_field_ids": [],
        },
    }
    write_json("smart_fill_group_mapping.json", mapping_payload)

    contract_payload = {
        "contract_id": "smart-fill-group-output-v1",
        "schema_version": 1,
        "description": "五个并行抽取Item共用的结构化输出外壳。",
        "status_values": ["filled", "missing", "conflict", "invalid_format", "needs_review"],
        "confidence": {
            "enabled": False,
            "reason": "置信度公式按改造目标后置，基线阶段不冻结评分算法。",
        },
        "field_result": {
            "required": ["field_id", "status", "value", "evidence"],
            "properties": {
                "field_id": {"type": "string"},
                "status": {"enum": ["filled", "missing", "conflict", "invalid_format", "needs_review"]},
                "value": {
                    "description": "普通字段为标量，动态表格为对象数组；missing时为空字符串、null或空数组。"
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "required": ["file_id", "quote"],
                        "properties": {
                            "file_id": {"type": "string"},
                            "file_name": {"type": "string"},
                            "section": {"type": ["string", "null"]},
                            "page": {"type": ["integer", "null"]},
                            "paragraph_index": {"type": ["integer", "null"]},
                            "char_start": {"type": ["integer", "null"]},
                            "char_end": {"type": ["integer", "null"]},
                            "quote": {"type": "string"},
                        },
                    },
                },
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        },
        "group_output": {
            "required": ["group_id", "fields", "missing_field_ids", "warnings"],
            "properties": {
                "group_id": {"enum": list(ITEM_DEFINITIONS)},
                "fields": {"type": "array", "items": {"$ref": "field_result"}},
                "missing_field_ids": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        },
        "dynamic_value_contracts": {
            "financial_forecast_table": {
                "type": "array",
                "item_example": {"year": 2026, "revenue": 301275, "net_profit": 22438},
                "export_mapping": "确认t0定义后再映射到t0至t0+7。",
            },
            "risk_table": {"type": "array", "must_preserve_all_rows": True},
            "target_company_equity_table": {"type": "array", "must_preserve_all_rows": True},
            "investor_table": {"type": "array", "must_preserve_all_rows": True},
            "non_financial_indicator_table": {"type": "array", "must_preserve_all_rows": True},
        },
        "open_decisions": [
            {
                "id": "t0_definition",
                "status": "pending_business_confirmation",
                "question": "t0表示投资发生年份，还是首个经营预测年份？",
                "blocking_for": ["excel_export_mapping"],
                "not_blocking_for": ["document_parse", "field_extract", "draft_review"],
            }
        ],
    }
    write_json("smart_fill_output_contract.json", contract_payload)

    manifest_payload = {
        "manifest_id": "smart-autofill-baseline-2026-07-12",
        "purpose": "第一阶段固定测试材料；当前仅记录文件事实，不把当前解析器结果当作新实现验收答案。",
        "single_user_test_mode": True,
        "files": [document_baseline(path) for path in BENCHMARK_FILES],
        "quality_baseline": {
            "smart_autofill_strict_accuracy": 0.703,
            "smart_autofill_completeness": 0.81,
            "prooof_new_strict_accuracy": 0.698,
            "prooof_new_completeness": 0.57,
            "source": str(Path(r"E:\MyProjects\改造目标.md")),
        },
        "phase_acceptance": {
            "required_extensions": [".pdf", ".docx"],
            "field_schema_id": schema_payload["schema_id"],
            "parallel_item_mapping_id": mapping_payload["mapping_id"],
            "output_contract_id": contract_payload["contract_id"],
        },
    }
    write_json("benchmark_manifest.json", manifest_payload)

    print(
        json.dumps(
            {
                "field_count": len(fields),
                "group_count": len(groups),
                "item_count": len(items),
                "manual_only_count": sum(
                    field["extraction_mode"] == "manual_only" for field in fields
                ),
                "benchmark_file_count": len(BENCHMARK_FILES),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
