from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from contract_ocr.service import ContractOcrError, ContractOcrService, PdfInspection



class RuntimeInfo(BaseModel):
    status: str
    service: str
    service_version: str
    paddleocr_version: str
    paddlepaddle_version: str
    model_initialized: bool


class OcrErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool


class PdfInspectionResponse(BaseModel):
    classification: str
    page_count: int
    text_page_count: int
    pages_requiring_ocr: list[int]
    source_sha256: str

class OcrStructureResponse(BaseModel):
    schema_version: str
    classification: str
    page_count: int
    pages_requiring_ocr: list[int]
    source_sha256: str
    pages: list[dict[str, Any]]



def _runtime_info(service: ContractOcrService, *, verify_import: bool) -> RuntimeInfo:
    paddleocr_version, paddlepaddle_version = service.runtime_versions(verify_import=verify_import)
    return RuntimeInfo(
        status="UP",
        service=service.service_name,
        service_version=service.service_version,
        paddleocr_version=paddleocr_version,
        paddlepaddle_version=paddlepaddle_version,
        model_initialized=service.model_initialized,
    )


def _inspection_response(inspection: PdfInspection) -> PdfInspectionResponse:
    return PdfInspectionResponse(
        classification=inspection.classification,
        page_count=inspection.page_count,
        text_page_count=inspection.text_page_count,
        pages_requiring_ocr=list(inspection.pages_requiring_ocr),
        source_sha256=inspection.source_sha256,
    )


def create_app(service: ContractOcrService | None = None) -> FastAPI:
    ocr_service = service or ContractOcrService.from_environment()
    app = FastAPI(title="Contract OCR", version=ocr_service.service_version)

    @app.exception_handler(ContractOcrError)
    async def contract_ocr_error_handler(_, exc: ContractOcrError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=OcrErrorResponse(code=exc.code, message=str(exc), retryable=exc.retryable).model_dump(),
        )

    @app.get("/health", response_model=RuntimeInfo)
    def health() -> RuntimeInfo:
        return _runtime_info(ocr_service, verify_import=True)

    @app.get("/v1/internal/ocr/runtime", response_model=RuntimeInfo)
    def runtime() -> RuntimeInfo:
        return _runtime_info(ocr_service, verify_import=True)

    @app.post("/v1/internal/ocr/inspect", response_model=PdfInspectionResponse)
    async def inspect_pdf(file: Annotated[UploadFile, File(...)]) -> PdfInspectionResponse:
        content = await ocr_service.read_pdf_upload(file)
        return _inspection_response(ocr_service.inspect_pdf(content))

    @app.post("/v1/internal/ocr/table-structure", response_model=OcrStructureResponse)
    async def recognize_table_structure(
        file: Annotated[UploadFile, File(...)],
    ) -> OcrStructureResponse:
        content = await ocr_service.read_pdf_upload(file)
        structured = ocr_service.structure_pdf(content)
        return OcrStructureResponse(
            schema_version="1.0",
            classification=structured.inspection.classification,
            page_count=structured.inspection.page_count,
            pages_requiring_ocr=list(structured.inspection.pages_requiring_ocr),
            source_sha256=structured.inspection.source_sha256,
            pages=structured.pages,
        )

    @app.post("/v1/internal/ocr/convert")
    async def convert_pdf(file: Annotated[UploadFile, File(...)]) -> Response:
        content = await ocr_service.read_pdf_upload(file)
        converted = ocr_service.convert_pdf(file.filename or "contract.pdf", content)
        return Response(
            content=converted.archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=contract-ocr-result.zip",
                "X-Contract-Ocr-Classification": converted.inspection.classification,
                "X-Contract-Ocr-Source-Sha256": converted.inspection.source_sha256,
            },
        )

    return app


app = create_app()
