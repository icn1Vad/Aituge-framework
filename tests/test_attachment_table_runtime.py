"""Regression for spreadsheet attachments passed into the policy Q&A code tool."""

import asyncio
import json
from pathlib import Path

from openpyxl import Workbook

from tool.artifacts import ArtifactRef
from tool.local_runtime.limited_local_python import (
    LimitedLocalPythonConfig,
    LimitedLocalPythonTool,
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, source_path: Path, *, sequence: int, mime: str) -> ArtifactRef:
        assert source_path.exists()
        self.published.append((source_path.name, mime))
        return ArtifactRef(
            id=f"artifact-{sequence}",
            name=source_path.name,
            mime=mime,
            url=f"/artifacts/{sequence}",
        )


def test_multi_sheet_attachment_calculation_and_chart(tmp_path):
    workbook_path = tmp_path / "file-table.xlsx"
    workbook = Workbook()
    budget = workbook.active
    budget.title = "预算"
    budget.append(["金额"])
    budget.append([100])
    settlement = workbook.create_sheet("决算")
    settlement.append(["金额"])
    settlement.append([900])
    workbook.save(workbook_path)

    publisher = RecordingPublisher()
    tool = LimitedLocalPythonTool(
        LimitedLocalPythonConfig(
            work_dir=tmp_path / "runs",
            input_files={"file-table.xlsx": workbook_path},
            artifact_publisher=publisher,
            cleanup_run_dir=True,
        )
    )
    result = asyncio.run(
        tool.aexecute(
            """
import pandas as pd
import matplotlib.pyplot as plt
book = pd.read_excel('file-table.xlsx', sheet_name=None)
values = [int(frame['金额'].sum()) for frame in book.values()]
print(sum(values))
plt.bar(list(book), values)
plt.savefig('chart.png')
"""
        )
    )
    payload = json.loads(result)
    assert payload["exit_code"] == 0, payload
    assert payload["stdout"] == "1000"
    assert payload["artifacts"][0]["name"] == "chart.png"
    assert publisher.published == [("chart.png", "image/png")]
