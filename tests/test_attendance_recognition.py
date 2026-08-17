from backend.attendance_recognition import parse_attendance_sheet


def test_parse_realistic_attendance_sheet() -> None:
    text = """AI智能体可信测评与安全保障发展论坛
签到表
单位名称 签到 参会人
85 北京流码科技有限公司
86 宋奕晨 北京八月瓜科技有限公司
87 博信诚
88 中国人寿财产保险股份有限公司
89 巩剑 人保财险北京
90 李楠
"""

    result = parse_attendance_sheet("签到表.pdf", text, "PADDLE_OCR")

    assert result.is_attendance_sheet is True
    assert result.numbered_row_count == 6
    assert result.signature_evidence_count >= 3
    assert "博信诚" in result.participant_names
    assert result.confidence == 1
    assert result.warnings == []


def test_blank_document_is_not_verified() -> None:
    result = parse_attendance_sheet("other.pdf", "普通费用说明，没有参会记录")

    assert result.is_attendance_sheet is False
    assert result.signature_evidence_count == 0
    assert "NOT_ATTENDANCE_SHEET" in result.warnings
    assert "NO_SIGNATURE_EVIDENCE" in result.warnings
