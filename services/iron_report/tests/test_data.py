from datetime import date
from pathlib import Path

import pytest

from iron_report.data import IronReportDataRepository
from iron_report.errors import IronReportError
from iron_report.schemas import ReportType


DATA_ROOT = Path(__file__).resolve().parents[1] / "demo_data"


def test_prepared_data_checksum_and_latest_snapshot() -> None:
    repository = IronReportDataRepository(DATA_ROOT)

    health = repository.health()
    snapshot = repository.build_agent_snapshot(ReportType.DAILY, date(2026, 7, 17))

    assert health["checksumsVerified"] is True
    assert health["rowCount"] == 260
    assert snapshot["report_date"] == "2026-07-17"
    assert snapshot["data_as_of_date"] == "2026-07-17"
    assert snapshot["metrics"]["close"] == 762.0
    assert snapshot["metrics"]["settlement"] == 761.0
    assert len(snapshot["daily_series"]) == 60
    assert snapshot["daily_series"][-1]["date"] == "2026-07-17"
    assert snapshot["daily_series_complete_through"] == "2026-07-17"
    assert set(snapshot["daily_series"][-1]) == {
        "date", "close", "settlement", "volume_contracts", "open_interest_contracts"
    }
    assert snapshot["global_monthly"] == {"fred_imf": [], "world_bank": []}
    assert snapshot["fallback_news"]["items"]


def test_weekly_snapshot_is_bounded_and_complete_through_report_data_date() -> None:
    repository = IronReportDataRepository(DATA_ROOT)

    snapshot = repository.build_agent_snapshot(ReportType.WEEKLY, date(2026, 7, 17))

    assert len(snapshot["daily_series"]) == 60
    assert snapshot["daily_series"][0]["date"] < snapshot["daily_series"][-1]["date"]
    assert snapshot["daily_series"][-1]["date"] == snapshot["data_as_of_date"]
    assert snapshot["daily_series_complete_through"] == snapshot["data_as_of_date"]
    assert set(snapshot["daily_series"][-1]) == {"date", "close", "settlement"}
    assert len(snapshot["global_monthly"]["fred_imf"]) == 12
    assert len(snapshot["global_monthly"]["world_bank"]) == 12


def test_non_trading_report_date_resolves_to_previous_data_date() -> None:
    repository = IronReportDataRepository(DATA_ROOT)

    validated = repository.validate_report_date(ReportType.DAILY, date(2026, 7, 12))

    assert validated.report_date == date(2026, 7, 12)
    assert validated.data_as_of_date == date(2026, 7, 10)


@pytest.mark.parametrize("requested", [date(2025, 6, 24), date(2026, 7, 18)])
def test_out_of_range_date_has_stable_error(requested: date) -> None:
    repository = IronReportDataRepository(DATA_ROOT)

    with pytest.raises(IronReportError) as caught:
        repository.validate_report_date(ReportType.DAILY, requested)

    assert caught.value.code == "IRON_REPORT_DATE_OUT_OF_RANGE"
    assert caught.value.status_code == 422
