from __future__ import annotations

import argparse
import json
from typing import Any

from proof.application.conflict_retrieval.taxonomy import infer_policy_category
from proof.application.conflict_retrieval.title_normalizer import normalize_policy_title
from proof.config import get_settings
from proof.infrastructure.postgres.repository import ProofRepository


LEGACY_CATEGORY_CODES = frozenset({"governance", "finance", "general"})


def reprocess_policy_metadata(repository, *, dry_run: bool = False) -> dict[str, Any]:
    rows = repository.list_policy_metadata()
    changed = 0
    title_updates = 0
    category_updates = 0
    for row in rows:
        normalized_title = normalize_policy_title(str(row.get("title") or ""))
        current_category = str(row.get("category_code") or "")
        inferred_category = infer_policy_category(str(row.get("title") or ""))
        category_code = (
            inferred_category
            if current_category in LEGACY_CATEGORY_CODES and inferred_category != "other"
            else None
        )
        title_changed = normalized_title != str(row.get("normalized_title") or "")
        category_changed = category_code is not None and category_code != current_category
        if not title_changed and not category_changed:
            continue
        changed += 1
        title_updates += int(title_changed)
        category_updates += int(category_changed)
        if not dry_run:
            repository.update_policy_metadata(
                str(row["id"]),
                normalized_title=normalized_title,
                category_code=category_code,
            )
    return {
        "dry_run": dry_run,
        "scanned_count": len(rows),
        "changed_count": changed,
        "normalized_title_update_count": title_updates,
        "category_update_count": category_updates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill deterministic Proof category and normalized-title metadata."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = reprocess_policy_metadata(
        ProofRepository(get_settings()),
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
