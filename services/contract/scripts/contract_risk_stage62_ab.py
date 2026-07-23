#!/usr/bin/env python3
"""Run the frozen stage-6.2 Legacy/Direct commercial-financial A/B experiment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT,
    ROOT / "backend",
    ROOT / "backend" / "single-agent",
    ROOT / "services" / "contract" / "src",
    ROOT / "services" / "contract" / "tests",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from llama_index.core.tools import FunctionTool

from agent.prompts import REACT_PROMPT
from common.system_constants import DEFAULT_TENANT_ID
from contract.risk.plan_builder import RiskReviewPlanBuilder
from risk_fixture_loader import EXPECTED_HASHES, load_fixed_risk_plan_input
from service.agent import single_agent_runner as runner_module
from service.agent.single_agent_runner import SingleAgentRunner
from services.contract.capabilities.register import CommercialTermsStageResult
from services.contract.capabilities.risk_review import (
    CommercialFinancialDirectReviewer,
    DirectReviewError,
    commercial_request_from_context,
)
from task_manager.output_parser import parse_json_output


LEGACY_SKILL = ROOT / "services" / "contract" / "capabilities" / "skills" / (
    "contract-commercial-terms"
) / "SKILL.md"


class CountingLlm:
    def __init__(self, inner: Any, calls: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def astream(self, *args, **kwargs):
        started = time.perf_counter()
        stream = await self._inner.astream(*args, **kwargs)
        record: dict[str, Any] = {
            "duration_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        self._calls.append(record)

        async def iterate():
            try:
                async for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage:
                        data = usage if isinstance(usage, dict) else vars(usage)
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            if data.get(key) is not None:
                                record[key] = data[key]
                    yield chunk
            finally:
                record["duration_ms"] = round((time.perf_counter() - started) * 1000)

        return iterate()


def _legacy_tools(value, counters: Counter):
    semantic = value.stage_result.semantic_ir.model_dump(mode="json")
    blocks = [item.model_dump(mode="json") for item in value.source_blocks]
    block_index = {item["block_id"]: index for index, item in enumerate(blocks)}

    async def contract_get_document(review_id: str, document_id: str) -> dict[str, Any]:
        counters["contract_get_document"] += 1
        return {
            "review_id": review_id,
            "document_id": document_id,
            "generation_id": value.generation_id,
            "generation_status": "SUCCEEDED",
            "block_count": len(blocks),
        }

    async def contract_get_ir(review_id: str, document_id: str) -> dict[str, Any]:
        counters["contract_get_ir"] += 1
        return {
            "review_id": review_id,
            "document_id": document_id,
            "generation_id": value.generation_id,
            "generation_status": "SUCCEEDED",
            "contract_ir": {
                "ir_version": "1.0",
                "our_party": value.our_party,
                "counterparty": value.counterparty,
                "contract_type": value.contract_type,
                **semantic,
            },
        }

    async def contract_get_blocks(
        review_id: str,
        document_id: str,
        block_ids: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        counters["contract_get_blocks"] += 1
        selected = set(block_ids or [])
        values = [item for item in blocks if not selected or item["block_id"] in selected]
        return {
            "review_id": review_id,
            "document_id": document_id,
            "generation_id": value.generation_id,
            "blocks": values[:limit],
        }

    async def contract_get_clause_context(
        review_id: str,
        document_id: str,
        block_id: str,
        before: int = 1,
        after: int = 1,
    ) -> dict[str, Any]:
        counters["contract_get_clause_context"] += 1
        index = block_index[block_id]
        return {
            "review_id": review_id,
            "document_id": document_id,
            "generation_id": value.generation_id,
            "target_block_id": block_id,
            "blocks": blocks[max(0, index - before) : index + after + 1],
        }

    return [
        FunctionTool.from_defaults(async_fn=contract_get_document),
        FunctionTool.from_defaults(async_fn=contract_get_ir),
        FunctionTool.from_defaults(async_fn=contract_get_blocks),
        FunctionTool.from_defaults(async_fn=contract_get_clause_context),
    ]


async def _run_legacy(value, *, tenant_id: str, model_id: str) -> dict[str, Any]:
    tool_calls: Counter = Counter()
    llm_calls: list[dict[str, Any]] = []
    original_factory = runner_module.create_llm

    def counting_factory(config):
        return CountingLlm(original_factory(config), llm_calls)

    runner_module.create_llm = counting_factory
    started = time.perf_counter()
    try:
        runner = SingleAgentRunner(
            tenant_id=tenant_id,
            default_model_id=model_id,
            system_prompt=REACT_PROMPT,
        )
        schema = CommercialTermsStageResult.model_json_schema()
        task_prompt = (
            LEGACY_SKILL.read_text("utf-8")
            + "\n\nThe final response must validate this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        result = await runner.chat(
            user_message=json.dumps(
                {
                    "schema_version": "1.0",
                    "review_id": value.review_id,
                    "attempt_no": value.attempt_no,
                    "document_id": value.document_id,
                    "perspective": value.perspective.value,
                    "our_party_name": value.our_party,
                    "contract_type": value.contract_type,
                    "review_attitude": value.review_attitude,
                },
                ensure_ascii=False,
            ),
            tools=_legacy_tools(value, tool_calls),
            model_id=model_id,
            user_id="stage62-ab",
            persist=False,
            task_prompt=task_prompt,
        )
    finally:
        runner_module.create_llm = original_factory
    duration_ms = round((time.perf_counter() - started) * 1000)
    message = (result.response.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content") or ""
    parsed = parse_json_output(content)
    if not parsed.ok or not isinstance(parsed.structured, dict):
        validation_error = "Legacy commercial_terms_review did not return one JSON object"
        artifact = None
        raw_artifact: dict[str, Any] | None = None
    else:
        raw_artifact = parsed.structured
        try:
            artifact = CommercialTermsStageResult.model_validate(raw_artifact)
            validation_error = None
        except Exception as exc:
            # A Legacy schema failure is itself an A/B observation. Do not repair,
            # normalize, or discard the raw model output on its behalf.
            artifact = None
            validation_error = f"{type(exc).__name__}: {exc}"
    usage = result.response.get("usage") or {}
    findings = artifact.findings if artifact is not None else (raw_artifact or {}).get("findings", [])
    evidences = (
        artifact.evidences if artifact is not None else (raw_artifact or {}).get("evidences", [])
    )
    risk_levels = Counter(
        item.risk_level if artifact is not None else item.get("risk_level")
        for item in findings
        if (item.risk_level if artifact is not None else item.get("risk_level")) is not None
    )
    return {
        "status": "VALID" if artifact is not None else "INVALID",
        "validation_error": validation_error,
        "duration_ms": duration_ms,
        "model_call_count": len(llm_calls),
        "tool_call_count": sum(tool_calls.values()),
        "tool_calls_by_name": dict(sorted(tool_calls.items())),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "llm_calls": llm_calls,
        "finding_count": len(findings),
        "evidence_count": len(evidences),
        "risk_levels": dict(sorted(risk_levels.items())),
        "artifact": (
            artifact.model_dump(mode="json") if artifact is not None else raw_artifact
        ),
    }


async def _run_direct(
    value,
    *,
    tenant_id: str,
    model_id: str,
    repetitions: int,
) -> list[dict[str, Any]]:
    plan = RiskReviewPlanBuilder().build(value)
    contexts = [item for item in plan.contexts if item.unit_id == "commercial_financial"]
    if len(contexts) != 1:
        raise RuntimeError("The fixed commercial_financial Plan must contain exactly one Context")
    request = commercial_request_from_context(contexts[0])
    reviewer = CommercialFinancialDirectReviewer()
    results = []
    for index in range(1, repetitions + 1):
        started = time.perf_counter()
        try:
            result = await reviewer.review(
                request,
                tenant_id=tenant_id,
                model_id=model_id,
                framework_run_id=f"stage62-direct-{index}",
            )
            results.append(result.model_dump(mode="json"))
        except DirectReviewError as exc:
            diagnostics = [
                item.model_dump(mode="json") for item in exc.attempt_diagnostics
            ]
            results.append(
                {
                    "status": "FAILED",
                    "error_code": exc.code,
                    "error_message": str(exc),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "model_call_count": len(diagnostics),
                    "repair_count": max(0, len(diagnostics) - 1),
                    "tool_call_count": 0,
                    "attempt_diagnostics": diagnostics,
                    "cf005_candidate": (
                        exc.cf005_candidate.model_dump(mode="json")
                        if exc.cf005_candidate is not None
                        else None
                    ),
                }
            )
            break
    return results


def _summary(legacy: dict[str, Any], direct: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [item["duration_ms"] for item in direct]
    return {
        "legacy": {
            key: value for key, value in legacy.items() if key != "artifact"
        },
        "direct_repetitions": [
            (
                {
                    "status": "FAILED",
                    "error_code": item["error_code"],
                    "error_message": item["error_message"],
                    "duration_ms": item["duration_ms"],
                    "model_call_count": item["model_call_count"],
                    "repair_count": item["repair_count"],
                    "tool_call_count": item["tool_call_count"],
                    "attempt_diagnostics": item["attempt_diagnostics"],
                    "cf005_candidate": item["cf005_candidate"],
                }
                if item["status"] == "FAILED"
                else {
                    "status": item["status"],
                "duration_ms": item["duration_ms"],
                "ttft_ms": [metric["time_to_first_token_ms"] for metric in item["call_metrics"]],
                "prompt_tokens": item["prompt_tokens"],
                "cached_tokens": item["cached_tokens"],
                "completion_tokens": item["completion_tokens"],
                "total_tokens": item["total_tokens"],
                "model_call_count": item["model_call_count"],
                "repair_count": item["repair_count"],
                "repair_reasons": item["repair_reasons"],
                "schema_normalization_applied": item[
                    "schema_normalization_applied"
                ],
                "schema_normalization_type": item["schema_normalization_type"],
                "attempt_diagnostics": item["attempt_diagnostics"],
                "reason_code_enrichment_count": item[
                    "reason_code_enrichment_count"
                ],
                "reason_code_rule_version": item["reason_code_rule_version"],
                "ignored_model_reason_code_count": item[
                    "ignored_model_reason_code_count"
                ],
                "tool_call_count": item["tool_call_count"],
                "finding_count": len(item["findings"]),
                "evidence_count": sum(
                    len(finding["evidence_candidates"]) for finding in item["findings"]
                ),
                "check_codes": [check["check_code"] for check in item["check_results"]],
                "check_statuses": {
                    check["check_code"]: check["status"]
                    for check in item["check_results"]
                },
                "reason_codes": {
                    check["check_code"]: check["reason_code"]
                    for check in item["check_results"]
                },
                "decision_notes": {
                    check["check_code"]: check["decision_note"]
                    for check in item["check_results"]
                },
                "cf005_candidate": item["cf005_candidate"],
                "cf005_finding_count": sum(
                    1 for finding in item["findings"] if finding["check_code"] == "CF-005"
                ),
                }
            )
            for item in direct
        ],
        "direct_duration_ms": {
            "min": min(durations),
            "median": round(statistics.median(durations)),
            "max": max(durations),
        },
    }


async def _main(args) -> None:
    fixture_dir = args.fixture_dir.resolve(strict=True)
    for name, expected in EXPECTED_HASHES.items():
        actual = hashlib.sha256((fixture_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Fixture hash mismatch: {name}")
    value = load_fixed_risk_plan_input(fixture_dir)
    semantic = value.stage_result.semantic_ir.model_dump(mode="json")
    if sum(len(items) for items in semantic.values()) != 101 or len(value.source_blocks) != 93:
        raise RuntimeError("The fixed stage-6 fixture does not contain 101 IR and 93 Blocks")

    legacy = (
        json.loads(args.legacy_result.read_text("utf-8"))
        if args.legacy_result is not None
        else await _run_legacy(value, tenant_id=args.tenant_id, model_id=args.model_id)
    )
    direct = await _run_direct(
        value,
        tenant_id=args.tenant_id,
        model_id=args.model_id,
        repetitions=args.direct_repetitions,
    )
    artifact = {
        "artifact_type": "CONTRACT_RISK_STAGE62_AB_V1",
        "fixture_hashes": EXPECTED_HASHES,
        "input": {
            "ir_item_count": 101,
            "block_count": 93,
            "window_count": 12,
            "perspective": value.perspective.value,
            "review_attitude": value.review_attitude,
            "our_party": value.our_party,
            "counterparty": value.counterparty,
        },
        "legacy": legacy,
        "direct": direct,
        "summary": _summary(legacy, direct),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.write_text(text, "utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                **artifact["summary"],
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--legacy-result",
        type=Path,
        help="Reuse the recorded one-time Legacy observation without invoking the model again.",
    )
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--model-id", default="deepseek-v4-pro")
    parser.add_argument("--direct-repetitions", type=int, default=5, choices=range(5, 21))
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
