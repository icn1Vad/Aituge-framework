#!/usr/bin/env python3
"""Replay Finding consolidation from previously persisted Framework artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

try:
    from services.contract.capabilities.finding_consolidation import FindingConsolidationEngine
except ModuleNotFoundError as exc:
    if exc.name != "services":
        raise
    from finding_consolidation import FindingConsolidationEngine


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--tenant-id", default="0")
    parser.add_argument("--model-id", required=True)
    return parser.parse_args()


async def _main() -> None:
    args = _arguments()
    payload = json.loads(args.source.read_text(encoding="utf-8"))
    artifacts = payload.get("artifacts", payload)
    if isinstance(artifacts, list):
        artifacts = {
            item["artifact_type"]: item["content_json"]
            for item in artifacts
            if isinstance(item, dict)
            and isinstance(item.get("artifact_type"), str)
            and isinstance(item.get("content_json"), dict)
        }
    result = await FindingConsolidationEngine().consolidate(
        artifacts,
        tenant_id=args.tenant_id,
        model_id=args.model_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(_main())
