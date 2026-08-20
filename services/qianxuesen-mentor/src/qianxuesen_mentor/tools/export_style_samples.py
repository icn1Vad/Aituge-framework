from __future__ import annotations

import argparse
import json
from pathlib import Path

from qianxuesen_mentor.config import get_settings
from qianxuesen_mentor.repository import Repository


def main() -> None:
    parser = argparse.ArgumentParser(description="Export source-grounded Qian Xuesen style samples")
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    samples = Repository(get_settings()).style_samples(max(30, min(args.limit, 50)))
    if len(samples) < 30:
        raise SystemExit("At least 30 authored/letter chunks are required before style export")
    args.output.write_text("\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in samples) + "\n", "utf-8")
    print(f"exported {len(samples)} style samples to {args.output}")


if __name__ == "__main__":
    main()
