from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from pypdf import PdfWriter

from qianxuesen_mentor.errors import QianXuesenError


class PaddleStructureOcr:
    """Page-scoped PP-StructureV3 adapter; model initialization is intentionally lazy."""

    def __init__(self, *, device: str = "cpu", formula_enabled: bool = False) -> None:
        self._pipeline: Any | None = None
        self._lock = threading.Lock()
        self._device = device
        self._formula_enabled = formula_enabled

    def recognize(self, page: Any) -> tuple[str, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="qxs-ocr-") as directory:
            page_path = Path(directory) / "page.pdf"
            writer = PdfWriter()
            writer.add_page(page)
            with page_path.open("wb") as output:
                writer.write(output)
            results = self._get_pipeline().predict(
                str(page_path), use_table_recognition=True,
                use_wired_table_cells_trans_to_html=True,
                use_wireless_table_cells_trans_to_html=True,
                format_block_content=False,
            )
            payloads = [_payload(result) for result in results]
        text_parts: list[str] = []
        for payload in payloads:
            for block in payload.get("parsing_res_list", []) or []:
                if isinstance(block, Mapping):
                    value = str(block.get("block_content") or block.get("rec_text") or "").strip()
                    if value:
                        text_parts.append(value)
        return "\n".join(text_parts), {"engine": "PP-StructureV3", "results": payloads}

    def _get_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline
        with self._lock:
            if self._pipeline is None:
                try:
                    from paddleocr import PPStructureV3
                    self._pipeline = PPStructureV3(
                        lang="ch",
                        device=self._device,
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                        use_textline_orientation=False,
                        use_seal_recognition=False,
                        use_table_recognition=True,
                        use_formula_recognition=self._formula_enabled,
                        use_chart_recognition=False,
                    )
                except Exception as exc:
                    raise QianXuesenError(
                        "ocr_runtime_unavailable", "PP-StructureV3 未安装或初始化失败，请在独立 OCR worker 环境安装 PaddleOCR 3.x",
                        status_code=503, details={"retryable": True},
                    ) from exc
        return self._pipeline


def _payload(result: Any) -> dict[str, Any]:
    value = getattr(result, "json", result)
    if callable(value):
        value = value()
    if isinstance(value, Mapping) and isinstance(value.get("res"), Mapping):
        value = value["res"]
    if not isinstance(value, Mapping):
        raise QianXuesenError("ocr_invalid_result", "OCR 返回格式无效", status_code=502)
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda item: item.tolist() if hasattr(item, "tolist") else str(item)))
