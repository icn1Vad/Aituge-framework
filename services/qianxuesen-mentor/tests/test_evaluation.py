from qianxuesen_mentor.evaluation import GOLDEN_QUESTIONS
from qianxuesen_mentor.tools.evaluate_answers import evaluate


def test_golden_set_has_one_hundred_unique_questions_and_required_categories():
    assert len(GOLDEN_QUESTIONS) == 100
    assert len({item.question for item in GOLDEN_QUESTIONS}) == 100
    categories = {item.category for item in GOLDEN_QUESTIONS}
    assert {"facts_dates", "quotes", "principles", "calculation_chart", "unsupported", "web_fallback"} <= categories


def test_release_gate_detects_identity_boilerplate_and_accepts_grounded_fixture():
    records = [{
        "id": item.id,
        "answer": "现有资料中没有找到足够依据" if item.must_abstain_if_unsupported else "从系统工程的观点看，应先明确目标和边界。",
        "source_quotes": [], "citations": [{"verified": True}], "first_sse_seconds": 1.0,
    } for item in GOLDEN_QUESTIONS]
    assert evaluate(records)["passed"] is True
    records[0]["answer"] = "作为一个人工智能，我来回答。"
    report = evaluate(records)
    assert report["passed"] is False
    assert report["style_violations"][0]["phrase"] == "作为一个人工智能"
