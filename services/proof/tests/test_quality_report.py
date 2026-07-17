from __future__ import annotations

from proof.application.quality_report import build_policy_quality_report


def test_report_contains_only_duplicate_missing_and_mixed_findings() -> None:
    policy = {
        "id": "policy-1",
        "title": "结构错误实验制度",
        "version": "1.0",
        "status": "draft",
        "document_id": "document-1",
        "structure_profile": "mixed",
        "structure_diagnostics": {
            "profiles_detected": ["decimal_outline", "article"],
            "regions": [{"profile": "decimal_outline"}, {"profile": "article"}],
        },
    }
    clauses = [
        clause(1, "1", "1 目的\n用于测试。", "decimal_outline"),
        clause(2, "2.1", "2.1 管理部门", "decimal_outline"),
        clause(3, "2.2", "2.2 执行部门", "decimal_outline"),
        clause(4, "3", "3 附则", "decimal_outline"),
        clause(5, "第一条", "第一条 第一项。", "article"),
        clause(6, "第二条", "第二条 第二项。", "article"),
        clause(7, "第二条", "第二条 重复编号。", "article"),
        clause(8, "第四条", "第四条 跳过第三条。", "article"),
    ]

    report = build_policy_quality_report(policy, clauses)

    assert report["finding_counts"] == {
        "duplicate_number": 1,
        "missing_number": 1,
        "mixed_structure": 1,
        "semantic_ambiguity": 0,
    }
    assert [item["type"] for item in report["findings"]] == [
        "duplicate_number",
        "missing_number",
        "mixed_structure",
    ]
    assert report["findings"][1]["missing_numbers"] == ["第3条"]
    assert report["report_version"] == "policy-quality-v2"


def test_report_merges_semantic_findings_in_existing_shape() -> None:
    policy = {
        "id": "policy-semantic",
        "title": "语义审校制度",
        "status": "draft",
        "document_id": "document-semantic",
        "structure_profile": "article",
    }
    report = build_policy_quality_report(
        policy,
        [clause(1, "第一条", "第一条 相关部门应及时处理。", "article")],
        semantic_audit={"status": "completed", "error_message": None},
        semantic_findings=[
            {
                "id": "unit-1",
                "quote": "相关部门应及时处理",
                "problem": "责任主体和处理时限不明确。",
                "suggestion": "明确责任部门和完成时限。",
                "clause_ordinal": 1,
                "clause_no_raw": "第一条",
            }
        ],
    )

    assert report["finding_counts"]["semantic_ambiguity"] == 1
    assert report["findings"][0] == {
        "type": "semantic_ambiguity",
        "id": "unit-1",
        "message": "责任主体和处理时限不明确。",
        "quote": "相关部门应及时处理",
        "suggestion": "明确责任部门和完成时限。",
        "clause_ordinal": 1,
        "clause_no_raw": "第一条",
    }


def test_report_detects_missing_decimal_sibling() -> None:
    policy = {
        "id": "policy-2",
        "title": "小数编号制度",
        "structure_profile": "decimal_outline",
    }
    report = build_policy_quality_report(
        policy,
        [
            clause(1, "5.1", "5.1 第一项", "decimal_outline"),
            clause(2, "5.3", "5.3 第三项", "decimal_outline"),
        ],
    )
    missing = [item for item in report["findings"] if item["type"] == "missing_number"]
    assert len(missing) == 1
    assert missing[0]["missing_numbers"] == ["5.2"]


def test_report_is_empty_for_continuous_articles() -> None:
    policy = {"id": "policy-3", "title": "正常制度", "structure_profile": "article"}
    report = build_policy_quality_report(
        policy,
        [
            clause(1, "第一条", "第一条 正文。", "article"),
            clause(2, "第二条", "第二条 正文。", "article"),
        ],
    )
    assert report["has_findings"] is False
    assert report["findings"] == []


def clause(ordinal: int, label: str, text: str, unit_type: str) -> dict:
    return {
        "clause_ordinal": ordinal,
        "clause_no_raw": label,
        "unit_type": unit_type,
        "text": text,
        "heading_path": [],
    }
