from __future__ import annotations

from contract.application.result_hash import compute_result_hash


def test_result_hash_matches_golden_vector() -> None:
    payload = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "business_task_id": "10001",
        "contract_version_id": "20001",
        "contract_profile": {
            "contract_type": "SERVICE",
            "party_a": {"name": "Party A"},
            "party_b": {"name": "Party B"},
            "perspective": "PARTY_B",
            "our_party": "Party B",
            "counterparty": "Party A",
            "review_attitude": "NEUTRAL",
        },
        "summary": {
            "overview": "No findings.",
            "high_count": 0,
            "medium_count": 0,
            "low_count": 0,
            "info_count": 0,
        },
        "findings": [],
        "evidences": [],
        "relationships": [],
    }

    result_hash, canonical = compute_result_hash(payload)

    assert canonical == (
        '{"business_task_id":"10001","contract_profile":{"contract_type":"SERVICE",'
        '"counterparty":"Party A","our_party":"Party B","party_a":{"name":"Party A"},'
        '"party_b":{"name":"Party B"},"perspective":"PARTY_B",'
        '"review_attitude":"NEUTRAL"},"contract_version_id":"20001","evidences":[],'
        '"findings":[],"relationships":[],"review_id":"review-1","schema_version":"1.0",'
        '"summary":{"high_count":0,"info_count":0,"low_count":0,"medium_count":0,'
        '"overview":"No findings."}}'
    )
    assert result_hash == "sha256:13b8f5bed9d4ba65f50c40577e5255eeb6274011d374dfaa668435f20b87bab1"


def test_result_hash_treats_legal_evidence_ids_as_a_set() -> None:
    payload = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "business_task_id": "task-1",
        "contract_version_id": "version-1",
        "contract_profile": {},
        "summary": {},
        "findings": [
            {
                "finding_id": "finding-1",
                "evidence_ids": ["evidence-1"],
                "legal_evidence_ids": ["legal-b", "legal-a"],
            }
        ],
        "evidences": [{"evidence_id": "evidence-1"}],
        "relationships": [],
    }
    first, _ = compute_result_hash(payload)
    payload["findings"][0]["legal_evidence_ids"].reverse()
    second, _ = compute_result_hash(payload)

    assert first == second
