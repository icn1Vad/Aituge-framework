from __future__ import annotations

import argparse
import json
from pathlib import Path

from proof.application.dataset_audit import DatasetAuditor
from proof.application.service import ProofService
from proof.config import Settings, get_settings
from proof.errors import ProofError


def import_dataset(
    settings: Settings,
    root: Path,
    *,
    dry_run: bool = False,
) -> dict:
    auditor = DatasetAuditor(root, max_upload_bytes=settings.max_upload_bytes)
    snapshot = auditor.audit(refresh=True)
    summary = {
        "candidate_file_count": snapshot["summary"]["candidate_file_count"],
        "ready_file_count": snapshot["summary"]["ready_file_count"],
        "created_count": 0,
        "reused_count": 0,
        "failed_count": 0,
        "clause_count": 0,
        "failures": [],
    }
    if dry_run:
        summary["failed_count"] = snapshot["summary"]["blocked_file_count"]
        summary["clause_count"] = snapshot["summary"]["clause_count"]
        summary["failures"] = [
            {
                "relative_path": item["relative_path"],
                "error_code": item.get("error_code", "dataset_scan_failed"),
            }
            for item in snapshot["files"]
            if item["scan_status"] == "blocked"
        ]
        return summary

    service = ProofService(settings, dataset_auditor=auditor)
    for item in snapshot["files"]:
        path = auditor.path_for_file(item["id"])
        try:
            result = service.ingest_policy(
                content=path.read_bytes(),
                filename=path.name,
                title=item["title"],
                level_code=item["level_code"],
                category_code=item["category_code"],
            )
        except ProofError as exc:
            summary["failed_count"] += 1
            summary["failures"].append(
                {
                    "relative_path": item["relative_path"],
                    "error_code": exc.code,
                    "ingestion_run_id": exc.details.get("ingestion_run_id"),
                }
            )
            continue
        if result["reused"]:
            summary["reused_count"] += 1
        else:
            summary["created_count"] += 1
        summary["clause_count"] += len(result["clauses"])
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and import the configured proof policy dataset.")
    parser.add_argument("--root", type=Path, help="Dataset root; defaults to PROOF_DATASET_ROOT.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    root = args.root or settings.resolved_dataset_root()
    if root is None:
        raise SystemExit("Dataset root is required through --root or PROOF_DATASET_ROOT.")
    result = import_dataset(settings, root, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
