from smart_autofill.normalization import EXTERNAL_DATA_FIELD_IDS, normalize_agent_fields


def test_external_data_fields_are_forced_to_frontend_safe_missing_values() -> None:
    fields = [
        {
            "field_id": field_id,
            "status": "filled",
            "value": "guessed",
            "evidence": [{"quote": "not authoritative"}],
        }
        for field_id in EXTERNAL_DATA_FIELD_IDS
    ]

    normalized = normalize_agent_fields(fields)

    assert len(normalized) == 8
    assert all(field["status"] == "missing" for field in normalized)
    assert all(field["value"] is None for field in normalized)
    assert all(field["evidence"] == [] for field in normalized)


def test_forecast_categories_are_normalized_without_moving_agent_years() -> None:
    fields = [
        {"field_id": "estimated_investment_time", "status": "filled", "value": "2025年11月"},
        {
            "field_id": "financial_forecast_table",
            "status": "filled",
            "value": [{
                "类别": "营业收入（万元）",
                "投资年份（t0）": "301,275",
                "t0+1": "301,275",
                "t0+2": "301,275",
                "t0+3": "301,275",
                "t0+4": "301,275",
                "t0+5": "301,275",
                "t0+6": "301,275",
                "t0+7": "301,275",
            }],
            "evidence": [{"quote": "计算期年份：2026年、2027年、2028年"}],
        },
    ]

    normalized = normalize_agent_fields(fields)
    forecast = next(field for field in normalized if field["field_id"] == "financial_forecast_table")
    row = forecast["value"][0]
    assert row["类别"] == "营业收入"
    assert row["投资年份（t0）"] == "301,275"
    assert row["t0+1"] == "301,275"
    assert row["t0+7"] == "301,275"


def test_text_values_are_limited_to_the_frontend_contract() -> None:
    normalized = normalize_agent_fields([{
        "field_id": "competitive_advantage",
        "status": "filled",
        "value": "优" * 329,
    }])

    assert len(normalized[0]["value"]) == 300
    assert normalized[0]["status"] == "filled"


def test_agent_aligned_forecast_is_not_shifted_a_second_time() -> None:
    normalized = normalize_agent_fields([{
        "field_id": "financial_forecast_table",
        "status": "filled",
        "value": [{"类别": "营业收入", "投资年份（t0）": None, "t0+1": "301,275"}],
        "evidence": [{"quote": "2026年营业收入301,275万元"}],
    }])

    row = normalized[0]["value"][0]
    assert row["投资年份（t0）"] is None
    assert row["t0+1"] == "301,275"
    assert row["t0+2"] == ""


def test_absence_based_negative_becomes_unclear() -> None:
    normalized = normalize_agent_fields([{
        "field_id": "is_employee_shareholding",
        "status": "filled",
        "value": "否",
        "evidence": [{"quote": "股权结构中未见员工持股安排"}],
        "warnings": ["文档未明确声明不涉及员工持股"],
    }])

    assert normalized[0]["value"] == "未明确"
    assert normalized[0]["evidence"] == []


def test_immediate_managing_unit_is_selected_from_affiliation_chain() -> None:
    normalized = normalize_agent_fields([{
        "field_id": "managing_unit",
        "status": "filled",
        "value": "中国航天科技集团有限公司",
        "evidence": [{"quote": "航天工程隶属于中国航天科技集团有限公司中国运载火箭技术研究院。"}],
    }])

    assert normalized[0]["value"] == "中国运载火箭技术研究院"
