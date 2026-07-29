from __future__ import annotations

from proof.application.similarity import (
    SimilarityThresholds,
    normalize_similarity_text,
    similarity_report,
)


def _candidate(text: str, *, policy_id: str = "old", version_seq: int = 0) -> dict:
    clauses = [item for item in text.split("\n") if item]
    return {
        "policy_id": policy_id,
        "title": "采购管理办法",
        "normalized_title": "采购管理",
        "version": f"v1.0.{version_seq}",
        "version_seq": version_seq,
        "status": "effective",
        "category_code": "procurement_supply",
        "text": text,
        "clauses": clauses,
    }


def test_normalization_folds_width_case_whitespace_and_punctuation() -> None:
    assert normalize_similarity_text(" ＡＢＣ，采购 制度！\n") == "abc采购制度"


def test_small_change_requires_decision_and_proposes_next_patch() -> None:
    old = "\n".join(
        [
            "第一条采购申请应当由部门负责人审批。",
            "第二条采购金额超过十万元应当集体决策。",
            "第三条采购资料保存期限为十年。",
            "第四条供应商应当完成准入审查。",
            "第五条采购结果应当及时归档。",
        ]
    )
    new = old.replace("十万元", "十二万元")

    report = similarity_report(
        title="采购管理办法（修订）",
        normalized_title="采购管理",
        category_code="procurement_supply",
        text=new,
        clauses=new.split("\n"),
        candidates=[_candidate(old, version_seq=9)],
        thresholds=SimilarityThresholds(),
    )

    assert report["status"] == "decision_required"
    assert report["candidates"][0]["current_version"] == "v1.0.9"
    assert report["candidates"][0]["proposed_version"] == "v1.1.0"
    assert report["candidates"][0]["estimated_change_percent"] <= 20


def test_clearly_different_text_does_not_require_decision() -> None:
    old = "第一条采购申请应当审批。\n第二条供应商应当准入。"
    new = "第一条网络账号必须实名申请。\n第二条信息系统应当每日备份。"

    report = similarity_report(
        title="信息安全管理制度",
        normalized_title="信息安全管理",
        category_code="information_security",
        text=new,
        clauses=new.split("\n"),
        candidates=[_candidate(old)],
        thresholds=SimilarityThresholds(),
    )

    assert report == {
        "status": "clear",
        "basis": "max_20_percent_change",
        "candidates": [],
        "allowed_decisions": [],
    }
