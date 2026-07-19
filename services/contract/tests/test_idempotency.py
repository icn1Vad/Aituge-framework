from __future__ import annotations

from contract.api.models import CreateReviewRequest
from contract.application.idempotency import (
    build_request_fingerprint,
    canonical_json,
    normalize_party_name,
    sha256_bytes,
)


def test_normalize_party_name_uses_frozen_unicode_rules() -> None:
    assert normalize_party_name(None) is None
    assert normalize_party_name("　某某\t单位 \n") == "某某 单位"
    assert normalize_party_name("   ") is None
    assert normalize_party_name("Ａ公司") == "A公司"
    assert normalize_party_name("Acme，Ltd.") == "Acme,Ltd."
    assert normalize_party_name("Acme、Ltd.") == "Acme、Ltd."


def test_request_fingerprint_matches_golden_vector() -> None:
    request = CreateReviewRequest(
        business_task_id="10001",
        contract_version_id="20001",
        perspective="PARTY_B",
        our_party_name="某某 单位",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        schema_version="1.0",
    )

    fingerprint, payload = build_request_fingerprint(
        tenant_id="1",
        user_id="1",
        request=request,
        file_sha256="sha256:" + "0" * 64,
    )

    assert canonical_json(payload) == (
        '{"business_task_id":"10001","contract_type":"AUTO",'
        '"contract_version_id":"20001",'
        '"file_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000",'
        '"our_party_name":"某某 单位","perspective":"PARTY_B",'
        '"review_attitude":"NEUTRAL","schema_version":"1.0",'
        '"tenant_id":"1","user_id":"1"}'
    )
    assert fingerprint == "sha256:9b26f390f4c578e88f83ccd7ca8c491d1ae00c9f09515c16e7c01d56677c413c"


def test_file_hash_uses_raw_bytes() -> None:
    assert sha256_bytes(b"a\r\nb") != sha256_bytes(b"a\nb")
