from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from qianxuesen_mentor.evaluation import GOLDEN_QUESTIONS


QUOTED = re.compile(r"[“\"]([^”\"]{2,200})[”\"]")
ABSTAIN_MARKERS = ("没有找到足够依据", "未找到可靠资料", "现有资料中没有", "无法核实", "不能编造")
FORBIDDEN_STYLE = ("绝绝子", "YYDS", "老铁", "家人们", "我亲眼见到", "我清楚记得")
IDENTITY_BOILERPLATE = ("基于公开资料构建", "作为一个人工智能", "作为AI")


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    expected = {item.id: item for item in GOLDEN_QUESTIONS}
    by_id = {str(item.get("id")): item for item in records}
    missing = sorted(set(expected) - set(by_id))
    fabricated_quotes = 0
    citation_total = citation_verified = unsupported_total = unsupported_abstained = 0
    style_violations: list[dict[str, str]] = []
    first_sse: list[float] = []
    for question_id, question in expected.items():
        record = by_id.get(question_id)
        if not record:
            continue
        answer = str(record.get("answer") or "")
        source_quotes = "\n".join(str(value) for value in record.get("source_quotes") or [])
        fabricated_quotes += sum(1 for quote in QUOTED.findall(answer) if quote not in source_quotes)
        for citation in record.get("citations") or []:
            citation_total += 1
            citation_verified += int(bool(citation.get("verified")))
        if question.must_abstain_if_unsupported:
            unsupported_total += 1
            unsupported_abstained += int(any(marker in answer for marker in ABSTAIN_MARKERS))
        for phrase in FORBIDDEN_STYLE + IDENTITY_BOILERPLATE:
            if phrase in answer:
                style_violations.append({"id": question_id, "phrase": phrase})
        latency = record.get("first_sse_seconds")
        if isinstance(latency, (int, float)):
            first_sse.append(float(latency))
    citation_precision = citation_verified / citation_total if citation_total else 0.0
    abstention_rate = unsupported_abstained / unsupported_total if unsupported_total else 0.0
    p95 = percentile(first_sse, 0.95) if first_sse else None
    gates = {
        "all_100_questions_present": not missing,
        "fabricated_quotes_zero": fabricated_quotes == 0,
        "citation_precision_gte_95pct": citation_precision >= 0.95,
        "unsupported_abstention_100pct": abstention_rate == 1.0,
        "style_violations_zero": not style_violations,
        "first_sse_p95_lt_6s": p95 is not None and p95 < 6,
    }
    return {
        "passed": all(gates.values()), "gates": gates, "missing_question_ids": missing,
        "fabricated_quote_count": fabricated_quotes, "citation_precision": citation_precision,
        "unsupported_abstention_rate": abstention_rate, "style_violations": style_violations,
        "first_sse_p95_seconds": p95,
    }


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * quantile + 0.999999) - 1))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate captured answers against the Qian Xuesen release gates")
    parser.add_argument("answers", type=Path, help="JSONL: id, answer, source_quotes, citations, first_sse_seconds")
    args = parser.parse_args()
    records = [json.loads(line) for line in args.answers.read_text("utf-8").splitlines() if line.strip()]
    report = evaluate(records)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
