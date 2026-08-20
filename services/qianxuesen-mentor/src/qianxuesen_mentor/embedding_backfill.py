from __future__ import annotations

import logging
from typing import Any

from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.model_clients import ModelClients
from qianxuesen_mentor.repository import Repository

LOGGER = logging.getLogger(__name__)


class EmbeddingBackfill:
    """Build vectors from existing chunks/cards without touching OCR-derived data."""

    def __init__(self, settings: Settings, repository: Repository, models: ModelClients) -> None:
        self.settings = settings
        self.repository = repository
        self.models = models

    def run(self, *, document_ids: set[str] | None = None, max_items: int | None = None) -> dict[str, Any]:
        cfg = self.models.embedding_config
        initial = self.repository.embedding_progress(
            profile_id=self.models.profile_id, model=cfg.model, dimensions=cfg.dimensions,
            document_ids=document_ids,
        )
        LOGGER.info(
            "Embedding 开始：模型=%s，维度=%s，切片=%s/%s，卡片=%s/%s",
            cfg.model, cfg.dimensions, initial["chunks_indexed"], initial["chunks_total"],
            initial["cards_indexed"], initial["cards_total"],
        )
        embedded = 0
        input_tokens = 0
        batches = 0
        for kind in ("chunk", "card"):
            while max_items is None or embedded < max_items:
                remaining = self.models.embedding_batch_size
                if max_items is not None:
                    remaining = min(remaining, max_items - embedded)
                if remaining <= 0:
                    break
                if kind == "chunk":
                    items = self.repository.pending_chunk_embeddings(
                        profile_id=self.models.profile_id, model=cfg.model, dimensions=cfg.dimensions,
                        limit=remaining, document_ids=document_ids,
                    )
                    texts = [item["content"] for item in items]
                else:
                    items = self.repository.pending_card_embeddings(
                        profile_id=self.models.profile_id, model=cfg.model, dimensions=cfg.dimensions,
                        limit=remaining, document_ids=document_ids,
                    )
                    texts = [item["text"] for item in items]
                if not items:
                    break
                vectors, batch_tokens = self.models.embed_with_usage(texts)
                if kind == "chunk":
                    self.repository.index_chunk_embeddings(
                        [(item["id"], vector) for item, vector in zip(items, vectors)],
                        profile_id=self.models.profile_id, model=cfg.model,
                    )
                else:
                    self.repository.index_card_embeddings(
                        [(item["evidence_type"], item["id"], vector)
                         for item, vector in zip(items, vectors)],
                        profile_id=self.models.profile_id, model=cfg.model,
                    )
                embedded += len(items)
                input_tokens += batch_tokens
                batches += 1
                if batches == 1 or batches % self.settings.embedding_progress_interval == 0:
                    LOGGER.info("Embedding 进度：新增=%s，批次=%s，API输入Token=%s", embedded, batches, input_tokens)
        self.repository.refresh_document_index_status(
            profile_id=self.models.profile_id, model=cfg.model, dimensions=cfg.dimensions,
            document_ids=document_ids,
        )
        final = self.repository.embedding_progress(
            profile_id=self.models.profile_id, model=cfg.model, dimensions=cfg.dimensions,
            document_ids=document_ids,
        )
        complete = (
            final["chunks_indexed"] == final["chunks_total"]
            and final["cards_indexed"] == final["cards_total"]
        )
        result = {
            "model": cfg.model, "dimensions": cfg.dimensions, "profile_id": self.models.profile_id,
            "new_embeddings": embedded, "api_input_tokens": input_tokens, "batches": batches,
            "complete": complete, **final,
        }
        LOGGER.info("Embedding 完成：%s", result)
        return result
