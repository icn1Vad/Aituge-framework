#!/usr/bin/env python3
"""Apply a saved consolidation artifact to saved review-stage artifacts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from contract.callback.models import FindingConsolidationArtifact
from contract.internal.service import REVIEW_ARTIFACT_MODELS
from contract.review import merge_review_stage_results, namespace_review_stage_result


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("decisions", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    source_payload = json.loads(args.source.read_text(encoding="utf-8"))
    rows = source_payload.get("artifacts", source_payload)
    if isinstance(rows, list):
        artifacts = {
            item["artifact_type"]: item["content_json"]
            for item in rows
            if isinstance(item, dict)
            and isinstance(item.get("artifact_type"), str)
            and isinstance(item.get("content_json"), dict)
        }
    else:
        artifacts = rows

    stages = []
    references: dict[tuple[str, str], str] = {}
    source_findings = 0
    source_evidences = 0
    for artifact_type, model in REVIEW_ARTIFACT_MODELS.items():
        raw = model.model_validate(artifacts[artifact_type])
        namespaced = namespace_review_stage_result(artifact_type, raw)
        stages.append(namespaced)
        source_findings += len(raw.findings)
        source_evidences += len(raw.evidences)
        references.update(
            {
                (artifact_type, source.finding_id): target.finding_id
                for source, target in zip(raw.findings, namespaced.findings, strict=True)
            }
        )

    base_findings, base_evidences = merge_review_stage_results(stages)
    consolidation = FindingConsolidationArtifact.model_validate(
        json.loads(args.decisions.read_text(encoding="utf-8"))
    )
    findings, evidences = merge_review_stage_results(
        stages,
        consolidation=consolidation,
        finding_reference_ids=references,
    )
    evidence_ids = {item.evidence_id for item in evidences}
    integrity_ok = all(
        set(finding.evidence_ids) <= evidence_ids
        and all(
            evidence.finding_id == finding.finding_id
            for evidence in evidences
            if evidence.evidence_id in finding.evidence_ids
        )
        for finding in findings
    )
    result = {
        "source_finding_count": source_findings,
        "source_evidence_count": source_evidences,
        "exact_merge_finding_count": len(base_findings),
        "exact_merge_evidence_count": len(base_evidences),
        "semantic_merge_finding_count": len(findings),
        "semantic_merge_evidence_count": len(evidences),
        "removed_duplicate_count": len(base_findings) - len(findings),
        "risk_counts": dict(sorted(Counter(item.risk_level.value for item in findings).items())),
        "evidence_integrity_ok": integrity_ok,
        "surviving_findings": [
            {
                "finding_id": item.finding_id,
                "category": item.category.value,
                "risk_level": item.risk_level.value,
                "title": item.title,
                "evidence_count": len(item.evidence_ids),
            }
            for item in findings
        ],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
