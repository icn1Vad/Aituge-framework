from backend.attendance_recognition import (
    parse_attendance_sheet,
    parse_attendance_structure,
)


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
    assert result.signed_participant_names == []
    assert result.confidence == 1
    assert result.warnings == []


def test_blank_document_is_not_verified() -> None:
    result = parse_attendance_sheet("other.pdf", "普通费用说明，没有参会记录")

    assert result.is_attendance_sheet is False
    assert result.signature_evidence_count == 0
    assert "NOT_ATTENDANCE_SHEET" in result.warnings
    assert "NO_SIGNATURE_EVIDENCE" in result.warnings


def test_structure_parser_uses_coordinate_tokens_beyond_flattened_html() -> None:
    texts = ["序号", "参会人", "单位名称", "签到"]
    boxes = [
        [40, 10, 80, 30],
        [180, 10, 260, 30],
        [360, 10, 480, 30],
        [580, 10, 660, 30],
    ]
    for index, sequence in enumerate(range(85, 112)):
        y = 50 + index * 40
        texts.extend([str(sequence), "张三", f"测试单位{sequence}", f"签名{sequence}"])
        boxes.extend([
            [45, y, 75, y + 20],
            [185, y, 245, y + 20],
            [365, y, 500, y + 20],
            [585, y, 665, y + 20],
        ])
    prediction = {
        "rec_texts": list(reversed(texts)),
        "rec_boxes": list(reversed(boxes)),
        "rec_scores": [0.99] * len(texts),
    }
    # Overlapping high-resolution tiles can return the same physical token
    # with a near-one-glyph vertical shift. This must not create extra rows.
    shifted_boxes = [
        [x1 + 2, y1 + 18, x2 + 2, y2 + 18]
        if y1 >= 50
        else [x1, y1, x2, y2]
        for x1, y1, x2, y2 in boxes
    ]
    shifted_prediction = {
        "rec_texts": list(reversed(texts)),
        "rec_boxes": list(reversed(shifted_boxes)),
        "rec_scores": [0.97] * len(texts),
    }
    structure = {
        "pages": [
            {
                "page_number": 1,
                "blocks": [
                    {
                        "text": "可信赋能智安同行发展论坛",
                        "bbox": [100, 0, 500, 8],
                    }
                ],
                "tables": [
                    {
                        "tile_index": 0,
                        "pred_html": "85至103的展平内容",
                        "table_ocr_pred": prediction,
                    },
                    {
                        "tile_index": 1,
                        "table_ocr_pred": shifted_prediction,
                    },
                ],
            }
        ]
    }

    result = parse_attendance_structure("签到表.pdf", structure)

    assert result.is_attendance_sheet is True
    assert result.numbered_row_count == 27
    assert result.signature_evidence_count == 27
    assert result.participant_names == ["张三"]
    assert result.signed_participant_names == ["张三"]
    assert result.meeting_title == "可信赋能智安同行发展论坛"
    assert result.warnings == []

def test_structure_parser_links_signatures_to_the_correct_participant() -> None:
    prediction = {
        "rec_texts": [
            "序号", "参会人", "单位名称", "签到",
            "1", "张三", "测试单位一", "",
            "2", "李四", "测试单位二", "李四签名",
        ],
        "rec_boxes": [
            [40, 10, 80, 30], [180, 10, 260, 30],
            [360, 10, 480, 30], [580, 10, 660, 30],
            [45, 50, 75, 70], [185, 50, 245, 70],
            [365, 50, 500, 70], [585, 50, 665, 70],
            [45, 90, 75, 110], [185, 90, 245, 110],
            [365, 90, 500, 110], [585, 90, 665, 110],
        ],
        "rec_scores": [0.99] * 12,
    }
    structure = {
        "pages": [{
            "page_number": 1,
            "blocks": [{"text": "项目会议签到表", "bbox": [100, 0, 500, 8]}],
            "tables": [{"table_ocr_pred": prediction}],
        }]
    }

    result = parse_attendance_structure("签到表.pdf", structure)

    assert result.participant_names == ["张三", "李四"]
    assert result.signed_participant_names == ["李四"]
