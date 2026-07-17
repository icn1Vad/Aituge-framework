from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from proof.application.dataset_audit import DatasetAuditor
from proof.application.ingestion import PolicyIngestionPipeline
from proof.application.structure import STRUCTURE_ENGINE_VERSION, extract_policy_structure
from proof.config import Settings, get_settings
from proof.infrastructure.parsers import PARSER_VERSION, parse_document
from proof.infrastructure.postgres.repository import ProofRepository


DEFAULT_PROFILES = frozenset({"decimal_outline", "mixed"})


def reprocess_dataset_structures(
    settings: Settings,
    root: Path,
    *,
    profiles: frozenset[str] = DEFAULT_PROFILES,
) -> list[dict[str, Any]]:
    repository = ProofRepository(settings)
    pipeline = PolicyIngestionPipeline(settings, repository)
    auditor = DatasetAuditor(root, max_upload_bytes=settings.max_upload_bytes)
    snapshot = auditor.audit(refresh=True)
    selected = [item for item in snapshot["files"] if item.get("structure_profile") in profiles]
    documents = repository.get_documents_by_content_hashes([item["content_hash"] for item in selected])
    results: list[dict[str, Any]] = []

    for item in selected:
        path = auditor.path_for_file(item["id"])
        parsed = parse_document(path)
        extraction = extract_policy_structure(parsed.blocks)
        diagnostics = {**extraction.diagnostics, "warnings": extraction.warnings}
        existing = documents.get(item["content_hash"])
        if existing:
            rebuilt = repository.rebuild_document_structure(
                content_hash=item["content_hash"],
                parser_version=PARSER_VERSION,
                chunker_version=STRUCTURE_ENGINE_VERSION,
                warnings=parsed.warnings,
                structure_profile=extraction.profile,
                structure_diagnostics=diagnostics,
                blocks=parsed.blocks,
                units=extraction.units,
            )
            results.append({"action": "rebuilt", "relative_path": item["relative_path"], **(rebuilt or {})})
            continue
        created = pipeline.ingest_policy(
            content=path.read_bytes(),
            filename=path.name,
            title=item["title"],
            level_code=item["level_code"],
            category_code=item["category_code"],
        )
        results.append(
            {
                "action": "ingested",
                "relative_path": item["relative_path"],
                "document_id": created["document"]["id"],
                "policy_id": created["policy"]["id"],
                "structure_profile": created["document"]["structure_profile"],
                "block_count": len(parsed.blocks),
                "clause_count": len(extraction.units),
                "ingestion_run_id": created["ingestion_run_id"],
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild derived chunks for selected structure profiles.")
    parser.add_argument("--root", type=Path, help="Dataset root; defaults to PROOF_DATASET_ROOT.")
    parser.add_argument(
        "--profiles",
        default=",".join(sorted(DEFAULT_PROFILES)),
        help="Comma-separated structure profiles (default: decimal_outline,mixed).",
    )
    args = parser.parse_args()
    settings = get_settings()
    root = (args.root or settings.resolved_dataset_root())
    if root is None:
        raise SystemExit("Dataset root is required through --root or PROOF_DATASET_ROOT.")
    profiles = frozenset(item.strip() for item in args.profiles.split(",") if item.strip())
    for result in reprocess_dataset_structures(settings, root, profiles=profiles):
        print(
            f"{result['action']}: {result['relative_path']} "
            f"[{result['structure_profile']}, {result['clause_count']} units]"
        )


if __name__ == "__main__":
    main()
