from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI
from pydantic import BaseModel


SERVICE_NAME = "contract-ocr"
SERVICE_VERSION = "0.1.0"


class RuntimeInfo(BaseModel):
    status: str
    service: str
    service_version: str
    paddleocr_version: str
    paddlepaddle_version: str
    model_initialized: bool


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"Required package is not installed: {name}") from exc


def _runtime_info(*, verify_import: bool) -> RuntimeInfo:
    paddleocr_version = _package_version("paddleocr")
    paddlepaddle_version = _package_version("paddlepaddle")
    if verify_import:
        # Import validation must not instantiate PPStructureV3: model initialization downloads weights and is
        # intentionally deferred until the document-processing protocol is implemented.
        import paddle  # noqa: F401
        import paddleocr  # noqa: F401
    return RuntimeInfo(
        status="UP",
        service=SERVICE_NAME,
        service_version=SERVICE_VERSION,
        paddleocr_version=paddleocr_version,
        paddlepaddle_version=paddlepaddle_version,
        model_initialized=False,
    )


app = FastAPI(title="Contract OCR", version=SERVICE_VERSION)


@app.get("/health", response_model=RuntimeInfo)
def health() -> RuntimeInfo:
    return _runtime_info(verify_import=True)


@app.get("/v1/internal/ocr/runtime", response_model=RuntimeInfo)
def runtime() -> RuntimeInfo:
    return _runtime_info(verify_import=True)
