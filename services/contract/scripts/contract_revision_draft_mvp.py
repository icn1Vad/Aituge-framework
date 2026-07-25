"""Generate Revision Draft MVP artifacts from a completed formal result."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from services.contract.capabilities.revision_drafts import (
    InMemoryRevisionSourceProvider,
    LlmRevisionTextGenerator,
    RevisionDraftService,
    _find_contract_ir_list,
    source_from_formal_payload,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--formal-payload", type=Path, required=True)
    parser.add_argument("--contract-ir", type=Path, required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _human_readable(source, result) -> str:
    finding_by_id = {item.finding_id: item for item in source.findings}
    lines = [
        "# 合同风险修订草案 MVP",
        "",
        f"- review_id: `{result.review_id}`",
        f"- generation_id: `{result.generation_id}`",
        f"- result_hash: `{result.result_hash}`",
        f"- status: `{result.status}`",
        f"- Finding总数: {len(source.findings)}",
        f"- 草案数: {len(result.drafts)}",
        f"- 失败数: {len(result.failed_findings)}",
        f"- 模型调用: {result.model_call_count}",
        f"- 耗时: {result.duration_ms}ms",
        "",
    ]
    for draft in result.drafts:
        finding = finding_by_id[draft.finding_id]
        lines.extend(
            [
                f"## {finding.title}",
                "",
                f"- finding_id: `{draft.finding_id}`",
                f"- operation: `{draft.operation}`",
                f"- revision_key: `{draft.revision_key}`",
                f"- change_reason: {draft.change_reason}",
                f"- original_text: {draft.original_text or '—'}",
                f"- replacement_text: {draft.replacement_text or '—'}",
                (
                    "- target: "
                    + json.dumps(
                        draft.target.model_dump(mode="json") if draft.target else None,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
                (
                    f"- unsupported_reason: {draft.unsupported_reason}"
                    if draft.unsupported_reason
                    else ""
                ),
                "",
            ]
        )
    if result.failed_findings:
        lines.extend(["## 失败Finding", ""])
        for item in result.failed_findings:
            lines.append(f"- `{item.finding_id}` `{item.error_code}`: {item.message}")
    return "\n".join(line for line in lines if line != "") + "\n"


async def _run(args: argparse.Namespace) -> int:
    payload = _load(args.formal_payload)
    ir_payload = _load(args.contract_ir)
    source = source_from_formal_payload(
        payload,
        generation_id=args.generation_id,
        contract_ir=_find_contract_ir_list(ir_payload),
    )
    provider = InMemoryRevisionSourceProvider(
        {(source.review_id, source.generation_id, source.result_hash): source}
    )
    service = RevisionDraftService(
        source_provider=provider,
        generator=LlmRevisionTextGenerator(
            tenant_id=args.tenant_id,
            model_id=args.model_id,
        ),
    )
    started = time.perf_counter()
    result = await service.get_or_generate(
        source.review_id,
        source.generation_id,
        source.result_hash,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / "stage-revision-draft-mvp.json"
    human_path = output / "stage-revision-draft-mvp-human-readable.md"
    attempts_path = output / "stage-revision-draft-mvp-attempts.json"
    _write_json(result_path, result.model_dump(mode="json"))
    human_path.write_text(_human_readable(source, result), encoding="utf-8")
    _write_json(
        attempts_path,
        {
            "schema_version": "1.0",
            "review_id": source.review_id,
            "generation_id": source.generation_id,
            "result_hash": source.result_hash,
            "status": result.status,
            "elapsed_ms": elapsed_ms,
            "model_call_count": result.model_call_count,
            "replace_count": sum(item.operation == "REPLACE" for item in result.drafts),
            "delete_count": sum(item.operation == "DELETE" for item in result.drafts),
            "unsupported_count": sum(item.operation == "UNSUPPORTED" for item in result.drafts),
            "failed_count": len(result.failed_findings),
            "formal_result_unchanged": True,
        },
    )
    manifest = {
        path.name: {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (result_path, human_path, attempts_path)
    }
    _write_json(output / "manifest.json", manifest)
    print(json.dumps({"result": result.model_dump(mode="json"), "manifest": manifest}, ensure_ascii=False))
    return 0 if result.status in {"COMPLETED", "PARTIAL_FAILED"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_arguments())))
