from __future__ import annotations

import argparse
import json
from typing import Any

from proof.application.semantic_audit import PolicyAuditService
from proof.config import get_settings
from proof.infrastructure.postgres.repository import ProofRepository


def reprocess_policy_audits(
    audit_service: PolicyAuditService,
    repository,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    skipped_complete = 0
    skipped_running = 0
    files = repository.list_files()

    for item in files:
        document_id = str(item["id"])
        run = repository.get_audit_run_for_document(document_id)
        statuses = (
            run.get("status") if run else None,
            run.get("summary_status") if run else None,
            run.get("conflict_status") if run else None,
        )
        if all(status == "completed" for status in statuses):
            skipped_complete += 1
            continue
        if any(status == "running" for status in statuses):
            skipped_running += 1
            continue
        candidates.append(
            {
                "document_id": document_id,
                "policy_id": str(item["policy_id"]),
                "previous_statuses": {
                    "semantic": statuses[0] or "not_created",
                    "summary": statuses[1] or "not_created",
                    "conflict": statuses[2] or "not_created",
                },
            }
        )

    selected = candidates[:limit] if limit is not None else candidates
    dispatched: list[dict[str, Any]] = []
    if not dry_run:
        for candidate in selected:
            state = audit_service.ensure_dispatched(candidate["document_id"])
            dispatched.append(
                {
                    **candidate,
                    "current_statuses": {
                        "semantic": state.get("status"),
                        "summary": (state.get("policy_summary") or {}).get("status"),
                        "conflict": (state.get("conflict_audit") or {}).get("status"),
                    },
                }
            )

    return {
        "dry_run": dry_run,
        "scanned_count": len(files),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "dispatched_count": len(dispatched),
        "skipped_complete_count": skipped_complete,
        "skipped_running_count": skipped_running,
        "items": selected if dry_run else dispatched,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill incomplete Proof summary, semantic, and conflict audit stages."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero")

    settings = get_settings()
    repository = ProofRepository(settings)
    result = reprocess_policy_audits(
        PolicyAuditService(settings, repository),
        repository,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
