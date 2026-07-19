from __future__ import annotations

import hashlib

import pytest

from contract.api.models import ContractProfile, Evidence, Finding
from contract.callback.models import EvidenceCandidate
from contract.errors import ContractError
from contract.evidence import materialize_evidence_set, validate_evidence_set


BLOCK_TEXT = "Payment is due in 30 days."


def test_validates_exact_text_evidence_against_its_contract_block() -> None:
    finding = _finding()
    evidence = _quote()

    validate_evidence_set([finding], [evidence], [_block()], _profile())


def test_materializes_quote_and_hash_from_a_block_range() -> None:
    candidate = EvidenceCandidate(
        evidence_id="evidence-1",
        finding_id="finding-1",
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        char_start=0,
        char_end=len(BLOCK_TEXT),
    )

    evidences = materialize_evidence_set([_finding()], [candidate], [_block()], _profile())

    assert evidences == [_quote()]


def test_rejects_optional_candidate_quote_that_does_not_match_the_block() -> None:
    wrong_text = "Payment is due in 60 days."
    candidate = EvidenceCandidate(
        evidence_id="evidence-1",
        finding_id="finding-1",
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        char_start=0,
        char_end=len(wrong_text),
        quoted_text=wrong_text,
        quoted_text_hash=_hash(wrong_text),
    )

    with pytest.raises(ContractError, match="candidate text"):
        materialize_evidence_set([_finding()], [candidate], [_block()], _profile())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"block_id": "another-block"}, "outside this contract generation"),
        ({"page_number": 3}, "page does not match"),
        ({"quoted_text": "Payment is due in 60 days."}, "text does not match"),
    ],
)
def test_rejects_text_evidence_not_supported_by_the_generation(change, message: str) -> None:
    payload = _quote().model_dump(mode="json")
    payload.update(change)
    if "quoted_text" in change:
        payload["char_end"] = len(change["quoted_text"])
        payload["quoted_text_hash"] = _hash(change["quoted_text"])
    evidence = Evidence.model_validate(payload)

    with pytest.raises(ContractError, match=message) as error:
        validate_evidence_set([_finding()], [evidence], [_block()], _profile())

    assert error.value.code == "EVIDENCE_INVALID"


def test_rejects_whitespace_only_absence_verification() -> None:
    finding = _finding(evidence_id="evidence-absence")
    evidence = Evidence(
        evidence_id="evidence-absence",
        finding_id=finding.finding_id,
        evidence_type="ABSENCE",
        checked_scope="ENTIRE_CONTRACT",
        verification_note=" ",
    )

    with pytest.raises(ContractError, match="verification note"):
        validate_evidence_set([finding], [evidence], [_block()], _profile())


def test_rejects_finding_party_mapping_that_differs_from_profile() -> None:
    payload = _finding().model_dump(mode="json")
    payload["our_party"] = "Acme Company"
    finding = Finding.model_validate(payload)

    with pytest.raises(ContractError, match="perspective, parties"):
        validate_evidence_set([finding], [_quote()], [_block()], _profile())


def _profile() -> ContractProfile:
    return ContractProfile(
        contract_type="SERVICE",
        party_a={"name": "Acme Company"},
        party_b={"name": "Beta Company"},
        perspective="PARTY_B",
        our_party="Beta Company",
        counterparty="Acme Company",
        review_attitude="NEUTRAL",
    )


def _finding(evidence_id: str = "evidence-1") -> Finding:
    return Finding(
        finding_id="finding-1",
        category="PAYMENT",
        risk_level="MEDIUM",
        title="Payment deadline",
        perspective="PARTY_B",
        our_party="Beta Company",
        counterparty="Acme Company",
        issue="Payment deadline is long.",
        impact_to_our_party="Payment may be delayed.",
        suggestion="Shorten the payment deadline.",
        evidence_ids=[evidence_id],
    )


def _quote() -> Evidence:
    return Evidence(
        evidence_id="evidence-1",
        finding_id="finding-1",
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        page_number=2,
        char_start=0,
        char_end=len(BLOCK_TEXT),
        quoted_text=BLOCK_TEXT,
        quoted_text_hash=_hash(BLOCK_TEXT),
    )


def _block() -> dict[str, object]:
    return {"block_id": "block-1", "page_number": 2, "text": BLOCK_TEXT}


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
