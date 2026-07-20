from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from contract.application.ports import UploadedContract
from contract.config import Settings
from contract.errors import ContractError
from contract.ir import ContractIR, build_structural_contract_ir
from contract.parser import ContractFileStore, NativeContractParser
from contract.persistence.models import DocumentBlockCreate, DocumentCreate
from contract.persistence.postgres.repository import ContractRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentProcessingResult:
    document_id: str
    generation_id: str
    generation_no: int
    document_reused: bool
    generation_reused: bool
    parse_completed: bool
    block_count: int
    structural_ir: dict[str, Any] | None


class ContractDocumentProcessor:
    """Persist the technical file, then create a non-active parse draft for Framework consumption."""

    def __init__(
        self,
        settings: Settings,
        repository: ContractRepository,
        *,
        file_store: ContractFileStore | None = None,
        parser: NativeContractParser | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.file_store = file_store or ContractFileStore(settings.resolved_data_dir())
        self.parser = parser or NativeContractParser()

    def process(
        self,
        *,
        upload: UploadedContract,
        document_id: str,
        generation_id: str,
        tenant_id: str,
        user_id: str,
        contract_version_id: str,
    ) -> DocumentProcessingResult:
        stored = self.file_store.store(upload, tenant_id=tenant_id, user_id=user_id)
        document, document_reused = self.repository.create_document(
            DocumentCreate(
                document_id=document_id,
                tenant_id=tenant_id,
                user_id=user_id,
                contract_version_id=contract_version_id,
                original_name=stored.original_name,
                content_type=stored.content_type,
                file_type=stored.file_type,
                file_size=stored.file_size,
                content_hash=stored.content_hash,
                storage_path=stored.relative_path,
            )
        )
        effective_document_id = document["id"]
        reservation = self.repository.reserve_parse_generation(
            generation_id=generation_id,
            document_id=effective_document_id,
            tenant_id=tenant_id,
            parser_version=self.parser.version,
        )
        generation = self.repository.get_parse_generation(
            reservation.generation_id,
            document_id=effective_document_id,
            tenant_id=tenant_id,
        )
        if generation is None:
            raise ContractError("INTERNAL_ERROR", "解析Generation创建后不可见", status_code=500)
        if reservation.completed or (
            generation["block_count"] > 0 and isinstance(generation["contract_ir_json"], dict)
        ):
            return self._result(
                document_id=effective_document_id,
                generation=generation,
                document_reused=document_reused,
                generation_reused=True,
            )

        source_path = self.file_store.resolve(document["storage_path"])
        try:
            parsed = self.parser.parse(source_path, generation_id=reservation.generation_id)
            structural_ir: ContractIR = build_structural_contract_ir(
                parsed,
                document_id=effective_document_id,
                generation_id=reservation.generation_id,
                content_hash=document["content_hash"],
                parser_version=self.parser.version,
            )
            block_values = [
                DocumentBlockCreate(
                    block_id=block.block_id,
                    block_no=block.block_no,
                    block_type=block.block_type,
                    text=block.text,
                    page_number=block.page_number,
                    paragraph_no=block.paragraph_no,
                    char_start=block.char_start,
                    char_end=block.char_end,
                    heading_path=list(block.heading_path),
                    metadata=dict(block.metadata),
                )
                for block in parsed.blocks
            ]
            staged, reused = self.repository.stage_parse_generation(
                generation_id=reservation.generation_id,
                document_id=effective_document_id,
                tenant_id=tenant_id,
                blocks=block_values,
                structural_ir=structural_ir.model_dump(mode="json"),
            )
        except ContractError as exc:
            self._fail_generation(reservation.generation_id, tenant_id=tenant_id, error_code=exc.code)
            raise
        except Exception as exc:
            self._fail_generation(
                reservation.generation_id,
                tenant_id=tenant_id,
                error_code="CONTRACT_PARSE_FAILED",
            )
            raise ContractError(
                "CONTRACT_PARSE_FAILED",
                "合同解析失败",
                status_code=422,
                user_action_required=True,
            ) from exc
        return self._result(
            document_id=effective_document_id,
            generation=staged,
            document_reused=document_reused,
            generation_reused=reservation.reused or reused,
        )

    @staticmethod
    def _result(
        *,
        document_id: str,
        generation: dict[str, Any],
        document_reused: bool,
        generation_reused: bool,
    ) -> DocumentProcessingResult:
        return DocumentProcessingResult(
            document_id=document_id,
            generation_id=generation["id"],
            generation_no=generation["generation_no"],
            document_reused=document_reused,
            generation_reused=generation_reused,
            parse_completed=generation["status"] == "SUCCEEDED",
            block_count=generation["block_count"],
            structural_ir=generation["contract_ir_json"],
        )

    def _fail_generation(self, generation_id: str, *, tenant_id: str, error_code: str) -> None:
        try:
            self.repository.fail_parse_generation(
                generation_id,
                tenant_id=tenant_id,
                error_code=error_code,
            )
        except Exception:
            logger.exception("Unable to mark parse generation %s as failed", generation_id)
