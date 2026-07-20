from __future__ import annotations

from typing import Any


class IronReportError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}


def date_out_of_range(*, requested: str, available_from: str, available_to: str) -> IronReportError:
    return IronReportError(
        "IRON_REPORT_DATE_OUT_OF_RANGE",
        "报告日期超出当前演示数据可生成范围",
        status_code=422,
        details={
            "requestedDate": requested,
            "availableFrom": available_from,
            "availableTo": available_to,
        },
    )
