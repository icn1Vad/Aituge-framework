from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from iron_report.errors import IronReportError, date_out_of_range
from iron_report.schemas import ReportType


@dataclass(frozen=True, slots=True)
class ValidatedReportDate:
    report_date: date
    data_as_of_date: date
    available_from: date
    available_to: date


class IronReportDataRepository:
    """Read-only access to the prepared, source-tracked demo dataset."""

    FUTURES_FILE = "processed/china_iron_ore_futures_i0_demo_260d.csv"
    GLOBAL_FRED_FILE = "processed/global_iron_ore_price_fred_demo_60m.csv"
    GLOBAL_WORLD_BANK_FILE = "processed/global_iron_ore_price_world_bank_demo_60m.csv"
    NEWS_FILE = "processed/news_snapshot.json"
    SOURCES_FILE = "metadata/sources.json"
    QUALITY_FILE = "quality/quality_report.json"

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._futures: pd.DataFrame | None = None

    def health(self) -> dict[str, Any]:
        self.verify_checksums()
        futures = self._load_futures()
        quality = self._read_json(self.QUALITY_FILE)
        return {
            "dataRoot": str(self.root),
            "rowCount": int(len(futures)),
            "dateMin": futures.iloc[0]["date"].date().isoformat(),
            "dateMax": futures.iloc[-1]["date"].date().isoformat(),
            "qualityStatus": quality.get("overall_status") or quality.get("status") or "UNKNOWN",
            "checksumsVerified": True,
        }

    def verify_checksums(self) -> None:
        manifest = self._require_file("SHA256SUMS")
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            expected, relative = line.split(maxsplit=1)
            path = self._require_file(relative.strip())
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                raise IronReportError(
                    "IRON_REPORT_DATA_CHECKSUM_MISMATCH",
                    "铁矿石演示数据校验失败",
                    status_code=500,
                    details={"file": relative.strip(), "expected": expected, "actual": actual},
                )

    def validate_report_date(self, report_type: ReportType, requested: date) -> ValidatedReportDate:
        frame = self._load_futures()
        lookback = 21 if report_type == ReportType.DAILY else 40
        available_from = frame.iloc[lookback - 1]["date"].date()
        available_to = frame.iloc[-1]["date"].date()
        if requested < available_from or requested > available_to:
            raise date_out_of_range(
                requested=requested.isoformat(),
                available_from=available_from.isoformat(),
                available_to=available_to.isoformat(),
            )
        eligible = frame.loc[frame["date"].dt.date <= requested]
        if len(eligible) < lookback:
            raise date_out_of_range(
                requested=requested.isoformat(),
                available_from=available_from.isoformat(),
                available_to=available_to.isoformat(),
            )
        data_date = eligible.iloc[-1]["date"].date()
        return ValidatedReportDate(requested, data_date, available_from, available_to)

    def build_agent_snapshot(self, report_type: ReportType, report_date: date) -> dict[str, Any]:
        validated = self.validate_report_date(report_type, report_date)
        frame = self._load_futures()
        frame = frame.loc[frame["date"].dt.date <= validated.data_as_of_date].copy()
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        returns = frame["close"].pct_change()
        metrics: dict[str, Any] = {
            "close": _number(latest["close"]),
            "settlement": _number(latest["settlement"]),
            "daily_return_pct": _pct(latest["close"] / previous["close"] - 1),
            "return_5d_pct": _period_return(frame, 5),
            "return_20d_pct": _period_return(frame, 20),
            "realized_volatility_20d_annualized_pct": _number(
                returns.tail(20).std(ddof=1) * math.sqrt(252) * 100
            ),
            "volume_contracts": int(latest["volume_contracts"]),
            "open_interest_contracts": int(latest["open_interest_contracts"]),
            "open_interest_change_5d": int(
                latest["open_interest_contracts"] - frame.iloc[-6]["open_interest_contracts"]
            ),
            "high_20d": _number(frame.tail(20)["high"].max()),
            "low_20d": _number(frame.tail(20)["low"].min()),
        }
        if report_type == ReportType.WEEKLY:
            current_week = frame.tail(5)
            previous_week = frame.iloc[-10:-5]
            metrics.update(
                {
                    "weekly_return_pct": _pct(current_week.iloc[-1]["close"] / current_week.iloc[0]["open"] - 1),
                    "weekly_high": _number(current_week["high"].max()),
                    "weekly_low": _number(current_week["low"].min()),
                    "weekly_average_volume": int(round(current_week["volume_contracts"].mean())),
                    "previous_week_average_volume": int(round(previous_week["volume_contracts"].mean())),
                }
            )
        daily_rows = frame.tail(60)
        if report_type == ReportType.WEEKLY:
            fred = self._load_global(self.GLOBAL_FRED_FILE, validated.data_as_of_date, limit=12)
            world_bank = self._load_global(self.GLOBAL_WORLD_BANK_FILE, validated.data_as_of_date, limit=12)
        else:
            fred = []
            world_bank = []
        return {
            "report_type": report_type.value,
            "report_date": validated.report_date.isoformat(),
            "data_as_of_date": validated.data_as_of_date.isoformat(),
            "market": {
                "symbol": str(latest["symbol"]),
                "market": str(latest["market"]),
                "currency": str(latest["currency"]),
                "price_unit": str(latest["price_unit"]),
                "source_id": str(latest["source_id"]),
            },
            "metrics": metrics,
            "daily_series": _records(daily_rows, report_type),
            "daily_series_order": "chronological_ascending",
            "daily_series_complete_through": validated.data_as_of_date.isoformat(),
            "global_monthly": {"fred_imf": fred, "world_bank": world_bank},
            "fallback_news": self.fallback_news(validated.report_date),
            "sources": self._read_json(self.SOURCES_FILE).get("sources", []),
            "limitations": [
                "国内行情为数据提供方构造的 I0 连续序列，不是单一可交割合约。",
                "全球价格为美元/吨，国内价格为人民币/吨；缺少汇率时不得直接计算跨市场价差。",
                "当前数据不包含港口库存、钢厂产量、运价和品位升贴水。",
                "新闻和公司公告仅用于背景说明，不构成行情预测。",
            ],
        }

    def fallback_news(self, report_date: date) -> dict[str, Any]:
        payload = self._read_json(self.NEWS_FILE)
        items = [
            item for item in payload.get("items", [])
            if not item.get("published_at") or date.fromisoformat(item["published_at"]) <= report_date
        ]
        return {"retrieved_at": payload.get("retrieved_at"), "items": items}

    def _load_futures(self) -> pd.DataFrame:
        if self._futures is None:
            path = self._require_file(self.FUTURES_FILE)
            frame = pd.read_csv(path, parse_dates=["date"])
            frame = frame.sort_values("date").reset_index(drop=True)
            required = {
                "date", "symbol", "market", "currency", "price_unit", "open", "high", "low",
                "close", "settlement", "volume_contracts", "open_interest_contracts", "source_id",
            }
            missing = sorted(required.difference(frame.columns))
            if missing:
                raise IronReportError(
                    "IRON_REPORT_DATA_INVALID",
                    "铁矿石行情数据缺少必填字段",
                    status_code=500,
                    details={"missingFields": missing},
                )
            if frame.empty or frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
                raise IronReportError("IRON_REPORT_DATA_INVALID", "铁矿石行情日期序列无效", status_code=500)
            self._futures = frame
        return self._futures

    def _load_global(self, relative: str, data_date: date, *, limit: int) -> list[dict[str, Any]]:
        frame = pd.read_csv(self._require_file(relative))
        frame["month_date"] = pd.to_datetime(frame["month"] + "-01")
        frame = frame.loc[frame["month_date"].dt.date <= data_date.replace(day=1)].tail(limit)
        return [
            {
                "month": str(row["month"]),
                "price_usd_per_metric_ton": _number(row["price_usd_per_metric_ton"]),
                "source_id": str(row["source_id"]),
            }
            for _, row in frame.iterrows()
        ]

    def _read_json(self, relative: str) -> dict[str, Any]:
        return json.loads(self._require_file(relative).read_text(encoding="utf-8"))

    def _require_file(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise IronReportError(
                "IRON_REPORT_DATA_NOT_FOUND",
                f"铁矿石演示数据文件不存在：{relative}",
                status_code=500,
            )
        return path


def _period_return(frame: pd.DataFrame, sessions: int) -> float:
    return _pct(frame.iloc[-1]["close"] / frame.iloc[-sessions - 1]["close"] - 1)


def _pct(value: Any) -> float:
    return _number(float(value) * 100)


def _number(value: Any) -> float:
    if pd.isna(value):
        return 0.0
    return round(float(value), 6)


def _records(frame: pd.DataFrame, report_type: ReportType) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        record = {
            "date": row["date"].date().isoformat(),
            "close": _number(row["close"]),
            "settlement": _number(row["settlement"]),
        }
        if report_type == ReportType.DAILY:
            record.update(
                {
                    "volume_contracts": int(row["volume_contracts"]),
                    "open_interest_contracts": int(row["open_interest_contracts"]),
                }
            )
        result.append(record)
    return result
