from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pypdf import PdfReader

from qianxuesen_mentor.cards import extract_cards
from qianxuesen_mentor.catalog import CATALOG, validate_catalog
from qianxuesen_mentor.chunking import build_chunks
from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.errors import QianXuesenError
from qianxuesen_mentor.ocr import PaddleStructureOcr
from qianxuesen_mentor.repository import Repository

if TYPE_CHECKING:
    from qianxuesen_mentor.model_clients import ModelClients


LOGGER = logging.getLogger(__name__)


class CorpusImporter:
    def __init__(self, settings: Settings, repository: Repository, *, models: "ModelClients | None" = None) -> None:
        self.settings = settings
        self.repository = repository
        self.models = models

    def catalog_only(self) -> dict[str, Any]:
        errors = validate_catalog(self.settings.resolved_corpus_root())
        count = self.repository.upsert_catalog()
        return {"documents": count, "pages": sum(entry.page_count for entry in CATALOG), "errors": errors}

    def import_all(self, *, resume: bool = True, use_ocr: bool = False, embed: bool = True,
                   document_ids: set[str] | None = None) -> list[dict[str, Any]]:
        catalog = self.catalog_only()
        if catalog["errors"]:
            raise QianXuesenError("corpus_invalid", "资料目录不完整", status_code=422,
                                  details={"errors": catalog["errors"]})
        results = []
        for entry in CATALOG:
            if document_ids and entry.id not in document_ids:
                continue
            results.append(self.import_document(entry.id, resume=resume, use_ocr=use_ocr, embed=embed))
        return results

    def import_document(self, document_id: str, *, resume: bool, use_ocr: bool, embed: bool) -> dict[str, Any]:
        document = self.repository.get_document(document_id)
        path = Path(document["storage_path"])
        reader = PdfReader(path)
        actual_pages = len(reader.pages)
        if actual_pages != int(document["page_count"]):
            raise QianXuesenError("page_count_mismatch", f"{path.name} 页数与清单不一致", status_code=422,
                                  details={"expected": document["page_count"], "actual": actual_pages})
        run_id = f"qxs-ingest-{uuid.uuid4().hex}"
        self.repository.create_run(run_id, document_id, actual_pages)
        statuses = self.repository.page_statuses(document_id) if resume else {}
        ocr = PaddleStructureOcr(
            device=self.settings.ocr_device,
            formula_enabled=self.settings.ocr_formula_enabled,
        ) if use_ocr else None
        processed = ocr_pages = failed = needs_ocr = blank_pages = 0
        LOGGER.info("开始处理 %s：%s 页，OCR=%s，设备=%s", path.name, actual_pages, use_ocr,
                    self.settings.ocr_device if use_ocr else "disabled")
        try:
            for page_no, page in enumerate(reader.pages, start=1):
                old = statuses.get(page_no)
                if resume and old in {"extracted", "ocr_success", "blank"}:
                    processed += 1
                    continue
                try:
                    native = sanitize_unicode(page.extract_text() or "")
                    score = text_quality(native)
                    if score >= 0.45 and visible_chars(native) >= self.settings.ocr_text_threshold:
                        self.repository.upsert_page(document_id, page_no, native_text=native, ocr_text="",
                                                    method="native", status="extracted", quality_score=score)
                    elif ocr is not None:
                        ocr_text, layout = ocr.recognize(page)
                        ocr_text = sanitize_unicode(ocr_text)
                        ocr_score = text_quality(ocr_text)
                        has_visible_text = visible_chars(ocr_text) > 0
                        status = "ocr_success" if has_visible_text else "blank"
                        self.repository.upsert_page(document_id, page_no, native_text=native, ocr_text=ocr_text,
                                                    method="ocr", status=status, quality_score=ocr_score,
                                                    layout=layout,
                                                    error_code=None if has_visible_text else "OCR_NO_TEXT")
                        ocr_pages += 1
                        blank_pages += 0 if has_visible_text else 1
                    else:
                        self.repository.upsert_page(document_id, page_no, native_text=native, ocr_text="",
                                                    method="pending_ocr", status="needs_ocr", quality_score=score)
                        needs_ocr += 1
                    processed += 1
                except Exception as exc:
                    failed += 1
                    error_code = getattr(exc, "code", type(exc).__name__)[:80]
                    LOGGER.warning("%s 第%s页处理失败：%s: %s", path.name, page_no, error_code, exc)
                    self.repository.upsert_page(document_id, page_no, native_text="", ocr_text="",
                                                method="failed", status="failed", quality_score=0,
                                                error_code=error_code)
                self.repository.update_run(run_id, current_page=page_no, processed_pages=processed,
                                           ocr_pages=ocr_pages, failed_pages=failed)
                if page_no == 1 or page_no % self.settings.ocr_progress_interval == 0 or page_no == actual_pages:
                    LOGGER.info("%s 进度 %s/%s，OCR=%s，空白=%s，待OCR=%s，失败=%s",
                                path.name, page_no, actual_pages, ocr_pages, blank_pages, needs_ocr, failed)
            pages = self.repository.document_pages(document_id)
            chunks = build_chunks(document_id, pages, max_chars=self.settings.chunk_max_chars,
                                  overlap=self.settings.chunk_overlap_chars)
            self.repository.replace_chunks(document_id, chunks)
            card_chunks = self.repository.chunks_for_cards(document_id)
            facts, principles = extract_cards(card_chunks)
            self.repository.replace_cards(document_id, facts, principles)
            index_status = "keyword_indexed" if chunks else "empty"
            if embed and self.models and chunks:
                vectors = self.models.embed([item["content"] for item in chunks])
                self.repository.index_chunk_embeddings(list(zip([item["id"] for item in chunks], vectors)),
                                                       profile_id=self.models.profile_id,
                                                       model=self.models.embedding_config.model)
                cards = self.repository.card_texts(document_id)
                card_vectors = self.models.embed([item["text"] for item in cards])
                self.repository.index_card_embeddings([
                    (item["evidence_type"], item["id"], vector) for item, vector in zip(cards, card_vectors)
                ], profile_id=self.models.profile_id, model=self.models.embedding_config.model)
                index_status = "indexed"
            ocr_status = "complete" if needs_ocr == 0 and failed == 0 else "required" if not use_ocr else "partial"
            self.repository.finish_document(document_id, sha256=file_sha256(path),
                                            ocr_status=ocr_status, index_status=index_status)
            self.repository.update_run(run_id, current_page=actual_pages, processed_pages=processed,
                                       ocr_pages=ocr_pages, failed_pages=failed, status="completed")
            LOGGER.info("完成 %s：切片=%s，事实=%s，原则=%s，失败=%s",
                        path.name, len(chunks), len(facts), len(principles), failed)
            return {"run_id": run_id, "document_id": document_id, "pages": actual_pages,
                    "chunks": len(chunks), "facts": len(facts), "principles": len(principles),
                    "blank": blank_pages, "needs_ocr": needs_ocr, "failed": failed,
                    "index_status": index_status}
        except KeyboardInterrupt:
            self.repository.update_run(run_id, current_page=processed, processed_pages=processed,
                                       ocr_pages=ocr_pages, failed_pages=failed, status="interrupted",
                                       error_summary="interrupted by operator")
            raise
        except Exception as exc:
            self.repository.update_run(run_id, current_page=processed, processed_pages=processed,
                                       ocr_pages=ocr_pages, failed_pages=failed, status="failed",
                                       error_summary=str(exc)[:2000])
            raise


def visible_chars(text: str) -> int:
    return sum(character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in text)


def sanitize_unicode(text: str) -> str:
    """Combine valid UTF-16 surrogate pairs and replace malformed isolated surrogates."""
    normalized = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return normalized.replace("\x00", "�")


def text_quality(text: str) -> float:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return 0.0
    return visible_chars(compact) / len(compact)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()
