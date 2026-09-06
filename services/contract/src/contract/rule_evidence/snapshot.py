"""Verified local snapshot of the existing Java rule library."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from functools import lru_cache

from contract.rule_evidence.models import ReviewRuleSnapshot, RuleEvidencePlanRequest
from contract.rule_evidence.planner import AdaptiveRuleEvidencePlanner


@lru_cache(maxsize=2)
def _shared_snapshot(directory, manifest_stamp, rules_stamp):
    # Registries are mounted for multiple agents. Keep only one verified copy
    # of the immutable release per process instead of 35k objects per registry.
    return LocalRuleSnapshot(directory)


def shared_rule_snapshot(directory):
    directory = Path(directory).resolve()
    def stamp(name):
        stat = (directory / name).stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    return _shared_snapshot(str(directory), stamp("manifest.json"), stamp("rules.ndjson"))


class LocalRuleSnapshot:
    def __init__(self, directory: str | Path):
        directory = Path(directory)
        self.manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
        digest = hashlib.sha256()
        rules = []
        identities = set()
        with (directory / "rules.ndjson").open("rb") as stream:
            for line in stream:
                digest.update(line)
                rule = ReviewRuleSnapshot.model_validate_json(line)
                if rule.rule_id in identities:
                    raise ValueError("Duplicate rule ID in snapshot")
                identities.add(rule.rule_id)
                rules.append(rule)
        if "sha256:" + digest.hexdigest() != self.manifest["snapshot_hash"]:
            raise ValueError("Rule snapshot SHA-256 mismatch")
        if len(rules) != self.manifest["record_count"]:
            raise ValueError("Rule snapshot count mismatch")
        self.rules = tuple(rules)

    def select(self, request: RuleEvidencePlanRequest) -> RuleEvidencePlanRequest:
        # Filter the complete release before applying the bounded request schema.
        selected = [rule for rule in self.rules
                    if AdaptiveRuleEvidencePlanner._applicable(rule, request)]
        payload = request.model_dump(mode="json")
        payload.update(
            source_version=self.manifest["source_version"],
            frozen_snapshot_hash=self.manifest["snapshot_hash"],
            rules=[rule.model_dump(mode="json") for rule in selected],
        )
        return RuleEvidencePlanRequest.model_validate(payload)
