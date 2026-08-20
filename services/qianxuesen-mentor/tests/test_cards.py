from qianxuesen_mentor.cards import extract_cards


def _chunk(kind: str):
    return {"id": f"c-{kind}", "content": "1955年10月，我国的系统工程实践需要总体设计。",
            "source_kind": kind, "page_start": 7, "page_end": 7}


def test_direct_source_fact_is_published_and_biography_fact_is_candidate():
    direct, principles = extract_cards([_chunk("authored")])
    secondary, _ = extract_cards([_chunk("biography")])
    assert direct[0]["card_status"] == "auto_published"
    assert secondary[0]["card_status"] == "candidate"
    assert principles[0]["constraints"].startswith("这是根据来源归纳")


def test_invalid_calendar_date_is_kept_as_text_without_date_value():
    chunks = [{
        "id": "c-invalid", "content": "材料写作1984年2月31日进行了讨论。",
        "source_kind": "authored", "page_start": 2, "page_end": 2,
    }]

    facts, _ = extract_cards(chunks)

    assert facts[0]["event_date"] is None
    assert "1984年2月31日" in facts[0]["value"]
