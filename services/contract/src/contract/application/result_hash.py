from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping

from contract.application.idempotency import canonical_json


RESULT_HASH_FIELDS = (
    "schema_version",
    "review_id",
    "business_task_id",
    "contract_version_id",
    "contract_profile",
    "summary",
    "findings",
    "evidences",
    "relationships",
)


def normalize_result_for_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {field: copy.deepcopy(value[field]) for field in RESULT_HASH_FIELDS}
    if value.get("legal_evidences"):
        normalized["legal_evidence_release_id"] = copy.deepcopy(
            value.get("legal_evidence_release_id")
        )
        normalized["legal_evidence_bundle_hash"] = copy.deepcopy(
            value.get("legal_evidence_bundle_hash")
        )
        normalized["legal_evidences"] = sorted(
            copy.deepcopy(value["legal_evidences"]),
            key=lambda item: item["evidence_id"],
        )
    normalized["findings"] = sorted(normalized["findings"], key=lambda item: item["finding_id"])
    normalized["evidences"] = sorted(normalized["evidences"], key=lambda item: item["evidence_id"])
    for finding in normalized["findings"]:
        finding["evidence_ids"] = sorted(finding["evidence_ids"])
        finding["legal_evidence_ids"] = sorted(finding.get("legal_evidence_ids", []))
    return normalized


def compute_result_hash(value: Mapping[str, Any]) -> tuple[str, str]:
    canonical = canonical_json(normalize_result_for_hash(value))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "sha256:" + digest, canonical
