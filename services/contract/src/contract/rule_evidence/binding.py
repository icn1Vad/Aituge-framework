from __future__ import annotations

import hashlib

from contract.application.idempotency import canonical_json
from contract.rule_evidence.models import RuleEvidenceBundle


RULE_EVIDENCE_BINDING_VERSION = "rule-evidence-check-binding-v1"


class RuleEvidenceBinder:
    profile_version = RULE_EVIDENCE_BINDING_VERSION

    def bind(self, bundle: RuleEvidenceBundle) -> RuleEvidenceBundle:
        issue_by_id = {item.issue_id: item for item in bundle.issues}
        evidence = []
        for item in bundle.evidence:
            issue_codes = {
                code
                for issue_id in item.issue_ids
                for code in issue_by_id[issue_id].check_codes
            }
            if item.rule.target_check_codes:
                issue_codes &= set(item.rule.target_check_codes)
            evidence.append(item.model_copy(update={"check_codes": sorted(issue_codes)}))

        bound_count = sum(bool(item.check_codes) for item in evidence)
        binding_status = (
            "NONE"
            if not evidence or bound_count == 0
            else "COMPLETE"
            if bound_count == len(evidence)
            else "PARTIAL"
        )
        payload = bundle.model_dump(mode="json")
        payload.update(
            {
                "binding_profile_version": self.profile_version,
                "binding_status": binding_status,
                "evidence": [item.model_dump(mode="json") for item in evidence],
            }
        )
        payload.pop("bundle_hash", None)
        return RuleEvidenceBundle(
            bundle_hash="sha256:"
            + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
            **payload,
        )
