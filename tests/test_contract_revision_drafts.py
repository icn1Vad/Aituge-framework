from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from services.contract.capabilities.revision_draft_api import create_app
from services.contract.capabilities.revision_drafts import (
    GeneratedReplacement,
    InMemoryRevisionDraftCache,
    InMemoryRevisionSourceProvider,
    PostgresRevisionSourceProvider,
    ReplacementBatchResult,
    RevisionDraft,
    RevisionDraftError,
    RevisionDraftService,
    RevisionEvidenceSource,
    RevisionFindingSource,
    RevisionInsertionTarget,
    RevisionIrSource,
    RevisionReviewSource,
    _normalize_append_literal_draft,
    _validate_replacement,
    compute_revision_hash,
    compute_revision_key,
)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_append_literal_draft_compiles_a_structured_numbering_plan() -> None:
    draft = RevisionDraft(
        revision_key=_hash("revision-key"),
        finding_id="finding-1",
        operation="SUPPLEMENT",
        replacement_text="6. 第一项\n7. 第二项\n（a）子项\n（1）更深子项",
        change_reason="补充完整条款",
        insertion_target=RevisionInsertionTarget(
            block_id="block-1",
            block_no=1,
            heading_path=["第一条"],
            anchor_excerpt="5. 原条款",
            anchor_text_hash=_hash("5. 原条款"),
            display_position="第一条之后",
        ),
        revision_hash=_hash("initial-revision"),
        validation_status="VALID",
        numbering_policy="APPEND_LITERAL",
    )

    normalized = _normalize_append_literal_draft(draft)

    assert normalized.replacement_text == "第一项\n第二项\n子项\n更深子项"
    assert [item.marker_type for item in normalized.numbering_plan] == [
        "DECIMAL",
        "DECIMAL",
        "PAREN_ALPHA",
        "PAREN_DECIMAL",
    ]
    assert [item.level for item in normalized.numbering_plan] == [0, 0, 1, 2]
    assert [item.marker for item in normalized.numbering_plan] == [
        "6.",
        "7.",
        "（a）",
        "（1）",
    ]
    assert normalized.numbering_plan[2].parent_item_id == normalized.numbering_plan[1].item_id
    assert normalized.numbering_plan[3].parent_item_id == normalized.numbering_plan[2].item_id
    assert normalized.revision_hash != draft.revision_hash


_INTERNAL_HEADERS = {
    "X-Internal-Service": "continew-java",
    "X-Internal-Token": "test-token",
    "X-Request-Id": "request-1",
}


@dataclass
class FakeGenerator:
    replacements: dict[str, str] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    async def generate(self, items, *, source):
        self.calls.append([item.revision_key for item in items])
        return ReplacementBatchResult(
            items=tuple(
                GeneratedReplacement(
                    revision_key=item.revision_key,
                    replacement_text=self.replacements.get(
                        item.finding.finding_id,
                        "双方变更服务范围的，应书面确认，并同步明确费用及履行期限。",
                    ),
                )
                for item in items
            )
        )


@dataclass
class FailSecondBatchGenerator(FakeGenerator):
    async def generate(self, items, *, source):
        if self.calls:
            self.calls.append([item.revision_key for item in items])
            raise TimeoutError("provider timed out")
        return await super().generate(items, source=source)


@dataclass
class FakeRevisionSnapshotRepository:
    snapshot: dict
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def get_revision_source_snapshot(self, review_id, *, tenant_id, user_id):
        self.calls.append((review_id, tenant_id, user_id))
        return self.snapshot


def _source(*findings: RevisionFindingSource, evidences=None, ir=None, result_hash=None):
    text = "甲方提出新增要求的，乙方应予执行。"
    evidence = RevisionEvidenceSource(
        evidence_id="evidence-1",
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        char_start=0,
        char_end=len(text),
        quoted_text=text,
        quoted_text_hash=_hash(text),
    )
    default_finding = RevisionFindingSource(
        finding_id="finding-1",
        title="单方变更权过宽",
        issue="甲方可单方提出新增要求，缺少书面变更和费用工期联动。",
        suggestion="新增需求应由双方书面确认，并同步调整费用和期限。",
        risk_level="MEDIUM",
        evidence_ids=["evidence-1"],
    )
    return RevisionReviewSource(
        review_id="review-1",
        generation_id="generation-1",
        result_hash=result_hash or _hash("result-1"),
        review_status="COMPLETED",
        perspective="PARTY_A",
        our_party="杭州戎一教育科技有限公司",
        counterparty="苏州爱兔格人工智能科技有限公司",
        findings=list(findings) or [default_finding],
        evidences=evidences or [evidence],
        contract_ir=ir
        or [
            RevisionIrSource(
                ir_id="I039",
                anchor_id="A025",
                block_id="block-1",
                char_start=0,
                char_end=len(text),
                extraction_text=text,
                context_text="服务变更",
            )
        ],
    )


def _service(source=None, generator=None, cache=None, *, max_batch_findings=6):
    value = source or _source()
    provider = InMemoryRevisionSourceProvider(
        {(value.review_id, value.generation_id, value.result_hash): value}
    )
    return RevisionDraftService(
        source_provider=provider,
        generator=generator or FakeGenerator(),
        cache=cache or InMemoryRevisionDraftCache(),
        max_batch_findings=max_batch_findings,
    )


@pytest.mark.asyncio
async def test_replace_links_to_finding_and_uses_python_target():
    result = await _service().get_or_generate("review-1", "generation-1", _hash("result-1"))
    draft = result.drafts[0]
    assert result.status == "COMPLETED"
    assert draft.finding_id == "finding-1"
    assert draft.operation == "REPLACE"
    assert draft.original_text == "甲方提出新增要求的，乙方应予执行。"
    assert draft.target.ir_id == "I039"
    assert draft.target.anchor_id == "A025"
    assert draft.target.block_id == "block-1"
    assert draft.target.quoted_text_hash == _hash(draft.original_text)


@pytest.mark.asyncio
async def test_review_generation_and_result_hash_are_strictly_validated():
    service = _service()
    with pytest.raises(RevisionDraftError, match="not found") as missing:
        await service.get_or_generate("missing", "generation-1", _hash("result-1"))
    assert missing.value.code == "REVIEW_NOT_FOUND"
    with pytest.raises(RevisionDraftError) as generation:
        await service.get_or_generate("review-1", "missing", _hash("result-1"))
    assert generation.value.code == "GENERATION_NOT_FOUND"
    with pytest.raises(RevisionDraftError) as result:
        await service.get_or_generate("review-1", "generation-1", _hash("different"))
    assert result.value.code == "RESULT_HASH_MISMATCH"


@pytest.mark.asyncio
async def test_postgres_source_uses_completed_result_generation_and_owner_scope():
    result_hash = _hash("persisted-result")
    payload = {
        "review_id": "review-1",
        "schema_version": "1.0",
        "contract_profile": {
            "perspective": "PARTY_A",
            "our_party": "Party A",
            "counterparty": "Party B",
        },
        "findings": [],
        "evidences": [],
        "relationships": [],
        "summary": {},
        "result_hash": result_hash,
    }
    repository = FakeRevisionSnapshotRepository(
        {
            "review_id": "review-1",
            "review_status": "SUCCEEDED",
            "result_hash": result_hash,
            "result_json": payload,
            "generation_id": "generation-1",
            "generation_status": "SUCCEEDED",
            "contract_ir_json": {
                "items": [
                    {
                        "ir_id": "I001",
                        "anchor_id": "A001",
                        "block_id": "B001",
                        "char_start": 0,
                        "char_end": 4,
                        "text": "text",
                    }
                ]
            },
        }
    )
    provider = PostgresRevisionSourceProvider(repository, tenant_id="tenant-1", user_id="user-1")

    source = await provider.get_source("review-1", "generation-1", result_hash)

    assert source.review_id == "review-1"
    assert source.generation_id == "generation-1"
    assert source.result_hash == result_hash
    assert await provider.resolve_generation_id("review-1", result_hash) == "generation-1"
    assert repository.calls == [
        ("review-1", "tenant-1", "user-1"),
        ("review-1", "tenant-1", "user-1"),
    ]

    with pytest.raises(RevisionDraftError) as generation:
        await provider.get_source("review-1", "generation-other", result_hash)
    assert generation.value.code == "GENERATION_NOT_FOUND"

    with pytest.raises(RevisionDraftError) as result:
        await provider.get_source("review-1", "generation-1", _hash("other"))
    assert result.value.code == "RESULT_HASH_MISMATCH"


@pytest.mark.asyncio
async def test_delete_is_deterministic_and_does_not_call_model():
    finding = _source().findings[0].model_copy(update={"preferred_operation": "DELETE"})
    generator = FakeGenerator()
    result = await _service(_source(finding), generator).get_or_generate(
        "review-1", "generation-1", _hash("result-1")
    )
    assert result.drafts[0].operation == "DELETE"
    assert result.drafts[0].replacement_text is None
    assert generator.calls == []


@pytest.mark.asyncio
async def test_absence_finding_is_unsupported_without_model_call():
    absence = RevisionEvidenceSource(
        evidence_id="absence-1",
        evidence_type="ABSENCE",
        checked_scope="合同全文",
    )
    finding = _source().findings[0].model_copy(update={"evidence_ids": ["absence-1"]})
    generator = FakeGenerator()
    result = await _service(_source(finding, evidences=[absence]), generator).get_or_generate(
        "review-1", "generation-1", _hash("result-1")
    )
    assert result.drafts[0].operation == "UNSUPPORTED"
    assert "缺失性证据" in result.drafts[0].unsupported_reason
    assert generator.calls == []


@pytest.mark.asyncio
async def test_multiple_discontinuous_text_evidences_are_unsupported():
    source = _source()
    second_text = "乙方应无条件配合。"
    second = RevisionEvidenceSource(
        evidence_id="evidence-2",
        evidence_type="TEXT_QUOTE",
        block_id="block-2",
        char_start=0,
        char_end=len(second_text),
        quoted_text=second_text,
        quoted_text_hash=_hash(second_text),
    )
    finding = source.findings[0].model_copy(
        update={"evidence_ids": ["evidence-1", "evidence-2"]}
    )
    generator = FakeGenerator()
    result = await _service(
        _source(finding, evidences=[source.evidences[0], second]), generator
    ).get_or_generate("review-1", "generation-1", _hash("result-1"))
    assert result.drafts[0].operation == "UNSUPPORTED"
    assert generator.calls == []


@pytest.mark.asyncio
async def test_multiple_findings_are_batched_not_called_one_by_one():
    base = _source()
    findings = []
    evidences = []
    ir = []
    for index in range(1, 8):
        text = f"甲方提出第{index}项新增要求的，乙方应执行。"
        evidences.append(
            RevisionEvidenceSource(
                evidence_id=f"evidence-{index}",
                evidence_type="TEXT_QUOTE",
                block_id=f"block-{index}",
                char_start=0,
                char_end=len(text),
                quoted_text=text,
                quoted_text_hash=_hash(text),
            )
        )
        findings.append(
            base.findings[0].model_copy(
                update={
                    "finding_id": f"finding-{index}",
                    "evidence_ids": [f"evidence-{index}"],
                }
            )
        )
        ir.append(
            RevisionIrSource(
                ir_id=f"I{index:03}",
                anchor_id=f"A{index:03}",
                block_id=f"block-{index}",
                char_start=0,
                char_end=len(text),
                extraction_text=text,
            )
        )
    generator = FakeGenerator()
    result = await _service(
        _source(*findings, evidences=evidences, ir=ir), generator
    ).get_or_generate("review-1", "generation-1", _hash("result-1"))
    assert len(result.drafts) == 7
    assert len(generator.calls) == 2
    assert [len(batch) for batch in generator.calls] == [6, 1]


@pytest.mark.asyncio
async def test_hash_mismatch_is_a_per_finding_source_failure():
    source = _source()
    invalid = source.evidences[0].model_copy(
        update={"quoted_text_hash": _hash("not-the-source")}
    )
    result = await _service(_source(evidences=[invalid])).get_or_generate(
        "review-1", "generation-1", _hash("result-1")
    )
    assert result.status == "FAILED"
    assert result.failed_findings[0].error_code == "SOURCE_TEXT_INVALID"


@pytest.mark.asyncio
async def test_empty_replacement_still_fails_as_a_structural_error():
    replacement = ""
    generator = FakeGenerator(replacements={"finding-1": replacement})
    result = await _service(generator=generator).get_or_generate(
        "review-1", "generation-1", _hash("result-1")
    )
    assert result.status == "FAILED"
    assert "empty" in result.failed_findings[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        "甲方提出新增要求的，乙方应予执行。",
        "双方应在TODO后确认。",
        "双方应于2028年1月1日书面确认。",
        "双方应支付100万元并书面确认。",
    ],
)
async def test_business_constraint_violation_accepts_first_structural_output(replacement):
    generator = FakeGenerator(replacements={"finding-1": replacement})
    result = await _service(generator=generator).get_or_generate(
        "review-1", "generation-1", _hash("result-1")
    )
    assert result.status == "COMPLETED"
    assert result.failed_findings == []
    assert result.drafts[0].replacement_text == replacement
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_named_party_removal_is_recorded_but_first_output_is_emitted(caplog):
    text = "杭州戎一教育科技有限公司应在收到通知后处理。"
    evidence = RevisionEvidenceSource(
        evidence_id="evidence-1",
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        char_start=0,
        char_end=len(text),
        quoted_text=text,
        quoted_text_hash=_hash(text),
    )
    ir = RevisionIrSource(
        ir_id="I001",
        anchor_id="A001",
        block_id="block-1",
        char_start=0,
        char_end=len(text),
        extraction_text=text,
    )
    generator = FakeGenerator(replacements={"finding-1": "我方应在收到通知后处理。"})
    result = await _service(
        _source(evidences=[evidence], ir=[ir]), generator
    ).get_or_generate("review-1", "generation-1", _hash("result-1"))
    assert result.status == "COMPLETED"
    assert result.failed_findings == []
    assert result.drafts[0].replacement_text == "我方应在收到通知后处理。"
    assert "revision_business_constraint_bypassed" in caplog.text
    assert "named contract party" in caplog.text


def test_replacement_validation_preserves_child_item_line_breaks():
    source = _source()
    original = "8.2 双方应按下列要求履行：\nA. 第一项；\nB. 第二项。"
    replacement = "8.2 双方应按下列要求履行：\r\nA. 第一项调整后；  \r\nB. 第二项调整后。  "

    assert _validate_replacement(replacement, original, source) == (
        "8.2 双方应按下列要求履行：\nA. 第一项调整后；\nB. 第二项调整后。"
    )


@pytest.mark.asyncio
async def test_one_failed_item_does_not_discard_other_drafts():
    source = _source()
    second_text = "甲方提出额外任务的，乙方应完成。"
    second_evidence = RevisionEvidenceSource(
        evidence_id="evidence-2",
        evidence_type="TEXT_QUOTE",
        block_id="block-2",
        char_start=0,
        char_end=len(second_text),
        quoted_text=second_text,
        quoted_text_hash=_hash(second_text),
    )
    second_finding = source.findings[0].model_copy(
        update={"finding_id": "finding-2", "evidence_ids": ["evidence-2"]}
    )
    second_ir = RevisionIrSource(
        ir_id="I002",
        anchor_id="A002",
        block_id="block-2",
        char_start=0,
        char_end=len(second_text),
        extraction_text=second_text,
    )
    generator = FakeGenerator(replacements={"finding-1": "", "finding-2": "额外任务须双方书面确认。"})
    result = await _service(
        _source(
            source.findings[0],
            second_finding,
            evidences=[source.evidences[0], second_evidence],
            ir=[source.contract_ir[0], second_ir],
        ),
        generator,
    ).get_or_generate("review-1", "generation-1", _hash("result-1"))
    assert result.status == "PARTIAL_FAILED"
    assert [item.finding_id for item in result.drafts] == ["finding-2"]
    assert [item.finding_id for item in result.failed_findings] == ["finding-1"]


@pytest.mark.asyncio
async def test_later_batch_timeout_returns_first_batch_without_retrying():
    source = _source()
    second_text = "甲方提出额外任务的，乙方应完成。"
    second_evidence = RevisionEvidenceSource(
        evidence_id="evidence-2",
        evidence_type="TEXT_QUOTE",
        block_id="block-2",
        char_start=0,
        char_end=len(second_text),
        quoted_text=second_text,
        quoted_text_hash=_hash(second_text),
    )
    second_finding = source.findings[0].model_copy(
        update={"finding_id": "finding-2", "evidence_ids": ["evidence-2"]}
    )
    second_ir = RevisionIrSource(
        ir_id="I002",
        anchor_id="A002",
        block_id="block-2",
        char_start=0,
        char_end=len(second_text),
        extraction_text=second_text,
    )
    generator = FailSecondBatchGenerator()
    result = await _service(
        _source(
            source.findings[0],
            second_finding,
            evidences=[source.evidences[0], second_evidence],
            ir=[source.contract_ir[0], second_ir],
        ),
        generator,
        max_batch_findings=1,
    ).get_or_generate("review-1", "generation-1", _hash("result-1"))
    assert result.status == "PARTIAL_FAILED"
    assert [item.finding_id for item in result.drafts] == ["finding-1"]
    assert [item.finding_id for item in result.failed_findings] == ["finding-2"]
    assert len(generator.calls) == 2


@pytest.mark.asyncio
async def test_cache_hit_does_not_repeat_model_call_and_result_hash_change_is_new_identity():
    generator = FakeGenerator()
    cache = InMemoryRevisionDraftCache()
    source1 = _source()
    source2 = _source(result_hash=_hash("result-2"))
    provider = InMemoryRevisionSourceProvider(
        {
            (source1.review_id, source1.generation_id, source1.result_hash): source1,
            (source2.review_id, source2.generation_id, source2.result_hash): source2,
        }
    )
    service = RevisionDraftService(provider, generator, cache)
    first = await service.get_or_generate("review-1", "generation-1", source1.result_hash)
    second = await service.get_or_generate("review-1", "generation-1", source1.result_hash)
    third = await service.get_or_generate("review-1", "generation-1", source2.result_hash)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert third.cache_hit is False
    assert len(generator.calls) == 2


def test_revision_key_and_revision_hash_are_stable_and_result_bound():
    key1 = compute_revision_key("r", "g", _hash("one"), "f")
    key2 = compute_revision_key("r", "g", _hash("one"), "f")
    key3 = compute_revision_key("r", "g", _hash("two"), "f")
    assert key1 == key2
    assert key1 != key3
    target = _service().source_provider.sources[("review-1", "generation-1", _hash("result-1"))]
    assert target.result_hash == _hash("result-1")
    assert compute_revision_hash(key1, "UNSUPPORTED", None, None, None) == compute_revision_hash(
        key1, "UNSUPPORTED", None, None, None
    )


def test_internal_api_returns_drafts_and_does_not_change_public_contract_api():
    source = _source()
    app = create_app(_service(source), internal_auth_enabled=False)
    client = TestClient(app)
    response = client.get(
        "/v1/internal/contract-reviews/review-1/revision-drafts",
        params={"generation_id": "generation-1", "result_hash": source.result_hash},
        headers=_INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["drafts"][0]["finding_id"] == "finding-1"
    assert "/v1/contract-reviews/{review_id}/result" not in app.openapi()["paths"]
    assert (
        "/v1/internal/contract-reviews/{review_id}/revision-drafts"
        not in app.openapi()["paths"]
    )


def test_internal_api_maps_domain_errors():
    app = create_app(_service(), internal_auth_enabled=False)
    response = TestClient(app).get(
        "/v1/internal/contract-reviews/missing/revision-drafts",
        params={"generation_id": "generation-1", "result_hash": _hash("result-1")},
        headers=_INTERNAL_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_internal_generate_endpoint_registers_source_then_get_uses_cache():
    provider = InMemoryRevisionSourceProvider({})
    generator = FakeGenerator()
    service = RevisionDraftService(provider, generator, InMemoryRevisionDraftCache())
    client = TestClient(create_app(service, internal_auth_enabled=False))
    source = _source()
    generated = client.post(
        "/v1/internal/contract-reviews/review-1/revision-drafts:generate",
        json=source.model_dump(mode="json"),
        headers=_INTERNAL_HEADERS,
    )
    assert generated.status_code == 200
    assert generated.json()["cache_hit"] is False
    queried = client.get(
        "/v1/internal/contract-reviews/review-1/revision-drafts",
        params={"generation_id": "generation-1", "result_hash": source.result_hash},
        headers=_INTERNAL_HEADERS,
    )
    assert queried.status_code == 200
    assert queried.json()["cache_hit"] is True
    assert len(generator.calls) == 1


def test_standalone_internal_api_fails_closed_without_valid_internal_token():
    source = _source()
    app = create_app(_service(source), internal_token="secret", internal_auth_enabled=True)
    client = TestClient(app)
    path = "/v1/internal/contract-reviews/review-1/revision-drafts"
    params = {"generation_id": "generation-1", "result_hash": source.result_hash}

    denied = client.get(
        path,
        params=params,
        headers={
            "X-Internal-Service": "continew-java",
            "X-Internal-Token": "wrong",
            "X-Request-Id": "request-1",
        },
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL_CALL"

    allowed = client.get(
        path,
        params=params,
        headers={
            "X-Internal-Service": "continew-java",
            "X-Internal-Token": "secret",
            "X-Request-Id": "request-1",
        },
    )
    assert allowed.status_code == 200
