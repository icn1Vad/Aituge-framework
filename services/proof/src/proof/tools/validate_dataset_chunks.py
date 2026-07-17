from __future__ import annotations

import argparse
import json
from pathlib import Path

from proof.application.structure_validation import DatasetStructureValidator
from proof.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate every dataset chunk against source blocks and PostgreSQL.")
    parser.add_argument("--root", type=Path, help="Dataset root; defaults to PROOF_DATASET_ROOT.")
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    args = parser.parse_args()

    settings = get_settings()
    root = args.root or settings.resolved_dataset_root()
    if root is None:
        raise SystemExit("Dataset root is required through --root or PROOF_DATASET_ROOT.")
    report = DatasetStructureValidator(settings, root).validate()
    payload = json.dumps(
        report,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        separators=(",", ":") if args.compact else None,
    )
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", "utf-8")
        print(output)
    else:
        print(payload)
    if report["summary"]["invalid_file_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
