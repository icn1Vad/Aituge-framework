from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from capabilities.register import ProofConflictItemOutput
from proof.api.app import _conflict_agent_view
from proof.application.service import ProofService
from proof.config import Settings


def _extract_json(text: str) -> dict[str, Any]:
    normalized = (text or "").strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        lines = lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:]
        normalized = "\n".join(lines).strip()
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Model output contains no JSON object.")
    return json.loads(normalized[start : end + 1])


def _shortlist(view: dict[str, Any], limit: int) -> dict[str, Any]:
    return {**view, "results": list(view.get("results") or [])[:limit]}


def run(unit_id: str, *, candidate_limit: int, output: Path) -> dict[str, Any]:
    settings = Settings()
    proof_service = ProofService(settings)
    retrieval_started = time.perf_counter()
    retrieval = proof_service.retrieve_conflict_candidates(unit_id, top_k=10)
    view = _shortlist(_conflict_agent_view(retrieval), candidate_limit)
    retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)

    skill_path = (
        Path(__file__).resolve().parents[3]
        / "capabilities"
        / "skills"
        / "proof-policy-conflict-audit"
        / "SKILL.md"
    )
    skill = skill_path.read_text(encoding="utf-8")
    system_prompt = "\n\n".join(
        [
            "You are the Proof policy conflict audit judge.",
            "The RAG evidence has already been fetched deterministically. Do not call tools.",
            skill,
        ]
    )
    user_prompt = "\n".join(
        [
            "Judge the source against itself and all supplied candidates.",
            "Return exactly the JSON object required by the skill.",
            "Prefetched conflict evidence:",
            json.dumps(view, ensure_ascii=False),
        ]
    )

    judge_started = time.perf_counter()
    response = httpx.post(
        settings.embedding_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        json={
            "model": settings.audit_model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 8000,
            "stream": False,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    content = str(body["choices"][0]["message"].get("content") or "")
    parsed = _extract_json(content)
    try:
        validated = ProofConflictItemOutput.model_validate(parsed)
    except ValidationError as exc:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "status": "validation_failed",
                    "unit_id": unit_id,
                    "candidate_limit": candidate_limit,
                    "usage": body.get("usage") or {},
                    "validation_errors": exc.errors(include_input=False),
                    "model_output": parsed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    for finding in validated.findings:
        proof_service._validate_conflict_finding(
            finding.model_dump(),
            target_ids={str(view["source"]["id"])},
        )
    judge_ms = int((time.perf_counter() - judge_started) * 1000)

    report = {
        "strategy": "deterministic_prefetch_single_joint_judge_no_thinking",
        "unit_id": unit_id,
        "candidate_limit": candidate_limit,
        "retrieval_ms": retrieval_ms,
        "judge_ms": judge_ms,
        "total_ms": retrieval_ms + judge_ms,
        "usage": body.get("usage") or {},
        "finding_count": len(validated.findings),
        "findings": [finding.model_dump() for finding in validated.findings],
        "retrieval": {
            "candidate_counts": view.get("candidate_counts"),
            "judge_candidate_ids": [item.get("id") for item in view.get("results") or []],
            "judge_order": view.get("judge_order"),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare a fixed one-call conflict Judge with ReAct.")
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--candidate-limit", type=int, default=10, choices=range(1, 13))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.unit_id, candidate_limit=args.candidate_limit, output=args.output)
    print(
        json.dumps(
            {
                "strategy": report["strategy"],
                "candidate_limit": report["candidate_limit"],
                "total_ms": report["total_ms"],
                "usage": report["usage"],
                "finding_count": report["finding_count"],
                "conflict_types": [item["conflict_type"] for item in report["findings"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
