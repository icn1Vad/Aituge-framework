from __future__ import annotations

import argparse
import json
import logging

from qianxuesen_mentor.config import get_settings
from qianxuesen_mentor.ingestion import CorpusImporter
from qianxuesen_mentor.repository import Repository


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Import the fixed Qian Xuesen PDF corpus")
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--no-embed", action="store_true")
    parser.add_argument("--document-id", action="append", default=[])
    args = parser.parse_args()
    settings = get_settings()
    repository = Repository(settings)
    if args.catalog_only:
        result = CorpusImporter(settings, repository).catalog_only()
    else:
        if args.no_embed:
            models = None
        else:
            from qianxuesen_mentor.model_clients import ModelClients
            models = ModelClients(settings)
        result = CorpusImporter(settings, repository, models=models).import_all(
            resume=args.resume, use_ocr=args.ocr, embed=not args.no_embed,
            document_ids=set(args.document_id) or None,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
