from __future__ import annotations

from contract.api.models import CreateReviewRequest, PartyResolutionCreateRequest
from contract.application.idempotency import (
    build_party_resolution_fingerprint,
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
        '{"business_task_id":"10001",'
        '"confirmed_party_a_name":null,"confirmed_party_b_name":null,'
        '"contract_type":"AUTO",'
        '"contract_version_id":"20001",'
        '"file_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000",'
            '"model_pack_id":"api-rerank",'
            '"our_party_name":"某某 单位","party_resolution_id":null,'
            '"perspective":"PARTY_B",'
        '"primary_playbook_id":null,"review_attitude":"NEUTRAL",'
        '"roles_by_playbook":{},"rule_release_id":null,"schema_version":"1.0",'
        '"selected_playbook_ids":["base_neutral"],'
        '"tenant_id":"1","user_id":"1"}'
    )
    assert fingerprint == "sha256:c7681874a9cae545b0464d878b79f6e70ce664ad0cfe286d4330e3d301c4b157"


def test_party_resolution_fingerprint_has_no_perspective() -> None:
    request = PartyResolutionCreateRequest(
        contract_version_id="20001",
        schema_version="1.0",
    )

    fingerprint = build_party_resolution_fingerprint(
        tenant_id="1",
        user_id="1",
        request=request,
        file_sha256="sha256:" + "0" * 64,
    )

    assert fingerprint == "sha256:d352fa95893a6d19ceeeb9668858d71acacab9eed54afee705fd5072fe8a0a89"


def test_file_hash_uses_raw_bytes() -> None:
    assert sha256_bytes(b"a\r\nb") != sha256_bytes(b"a\nb")
