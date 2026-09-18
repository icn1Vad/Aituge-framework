from business_workflow_kit.reimbursement import review_reimbursement_facts


def document(id, *, digest=None, md5=None, number=None, category="lodging", day="2026-09-18", traveler="甲"):
    return {
        "document_id": id,
        "file_sha256": digest,
        "file_md5": md5,
        "invoice_number": number,
        "category": category,
        "occurrence_date": day,
        "traveler": traveler,
    }


def test_duplicate_number_is_risk_even_when_files_differ():
    result = review_reimbursement_facts({"documents": [
        document("a", digest="a" * 64, number="001", day="2026-09-17"),
        document("b", digest="b" * 64, number="001"),
    ]})
    assert [item.status for item in result.records[:2]] == ["RISK", "RISK"]
    assert result.status == "RISK"


def test_existing_attachment_md5_detects_same_file():
    result = review_reimbursement_facts({"documents": [
        document("a", md5="a" * 32), document("b", md5="a" * 32, day="2026-09-17"),
    ]})
    assert [item.status for item in result.records[:2]] == ["RISK", "RISK"]


def test_no_history_is_pending_not_approved():
    result = review_reimbursement_facts({"documents": [document("a", digest="a" * 64)]})
    assert result.records[0].status == "PENDING"
    assert result.status == "PARTIAL"


def test_same_day_lodging_is_review_flag_not_fraud_verdict():
    result = review_reimbursement_facts({"documents": [
        document("a", digest="a" * 64), document("b", digest="b" * 64),
    ]})
    lodging = [row for row in result.records if row.check_code == "SAME_DAY_LODGING"]
    assert [row.status for row in lodging] == ["RISK", "RISK"]
    assert "核实" in lodging[0].message


def test_unrecognized_date_never_passes():
    result = review_reimbursement_facts({"documents": [
        document("a", digest="a" * 64, day="明天"),
    ]})
    assert result.records[-1].status == "PENDING"


def test_missing_documents_stays_partial():
    result = review_reimbursement_facts({"documents": []})
    assert result.status == "PARTIAL"


def test_bad_payload_fails_closed():
    result = review_reimbursement_facts({"documents": "model text"})
    assert result.status == "PARTIAL"
    assert all(row.status == "ERROR" for row in result.records)
