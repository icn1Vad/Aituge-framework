from __future__ import annotations

import argparse
import json
import logging

from qianxuesen_mentor.config import get_settings
from qianxuesen_mentor.embedding_backfill import EmbeddingBackfill
from qianxuesen_mentor.model_clients import ModelClients
from qianxuesen_mentor.repository import Repository


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill Qian Xuesen corpus embeddings without rerunning OCR")
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--max-items", type=int, default=None,
                        help="Stop after this many new vectors; useful for a connectivity smoke test")
    args = parser.parse_args()
    if args.max_items is not None and args.max_items <= 0:
        parser.error("--max-items must be positive")
    settings = get_settings()
    result = EmbeddingBackfill(
        settings, Repository(settings), ModelClients(settings),
    ).run(document_ids=set(args.document_id) or None, max_items=args.max_items)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
