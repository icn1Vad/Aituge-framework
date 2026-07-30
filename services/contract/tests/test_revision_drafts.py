from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from services.contract.capabilities.revision_drafts import (
    GeneratedReplacement,
    InMemoryRevisionDraftCache,
    InMemoryRevisionSourceProvider,
    ReplacementBatchResult,
    RevisionDraftResponse,
    RevisionDraftService,
    RevisionEvidenceSource,
    RevisionFindingSource,
    RevisionGenerationRequest,
    RevisionIrSource,
    RevisionReviewSource,
    SupplementRequest,
    source_from_formal_payload,
)


@dataclass
class _Generator:
    replacement_text: str
    calls: list[tuple[RevisionGenerationRequest, ...]] = field(default_factory=list)

    async def generate(
        self,
        items: Sequence[RevisionGenerationRequest],
        *,
        source: RevisionReviewSource,
    ) -> ReplacementBatchResult:
        self.calls.append(tuple(items))
        return ReplacementBatchResult(
            items=tuple(
                GeneratedReplacement(
                    revision_key=item.revision_key,
                    replacement_text=self.replacement_text,
                )
                for item in items
            )
        )


def _absence_source(*, verification_note: str | None = "已检查第六条，未发现违约后的补救安排。") -> RevisionReviewSource:
    result_hash = "sha256:" + "a" * 64
    return RevisionReviewSource(
        review_id="review-absence-1",
        generation_id="generation-absence-1",
        result_hash=result_hash,
        review_status="COMPLETED",
        perspective="PARTY_A",
        our_party="甲方公司",
        counterparty="乙方公司",
        findings=[
            RevisionFindingSource(
                finding_id="finding-absence-1",
                title="缺少违约补救约定",
                issue="合同未约定乙方违约后的补救义务。",
                suggestion="补充乙方违约后的补救责任。",
                risk_level="MEDIUM",
                evidence_ids=["evidence-absence-1"],
            )
        ],
        evidences=[
            RevisionEvidenceSource(
                evidence_id="evidence-absence-1",
                evidence_type="ABSENCE",
                checked_scope="第六条 违约责任",
                verification_note=verification_note,
            )
        ],
        contract_ir=[],
    )


def _replace_source() -> RevisionReviewSource:
    original = "乙方应按要求完成服务。"
    result_hash = "sha256:" + "b" * 64
    return RevisionReviewSource(
        review_id="review-replace-1",
        generation_id="generation-replace-1",
        result_hash=result_hash,
        review_status="COMPLETED",
        perspective="PARTY_A",
        our_party="甲方公司",
        counterparty="乙方公司",
        findings=[
            RevisionFindingSource(
                finding_id="finding-replace-1",
                title="服务期限不明确",
                issue="服务完成标准不够明确。",
                suggestion="明确乙方应按约完成服务并提交验收材料。",
                risk_level="LOW",
                evidence_ids=["evidence-text-1"],
            )
        ],
        evidences=[
            RevisionEvidenceSource(
                evidence_id="evidence-text-1",
                evidence_type="TEXT_QUOTE",
                block_id="block-1",
                char_start=0,
                char_end=len(original),
                quoted_text=original,
                quoted_text_hash="sha256:" + hashlib.sha256(original.encode("utf-8")).hexdigest(),
            )
        ],
        contract_ir=[
            RevisionIrSource(
                ir_id="ir-1",
                anchor_id="anchor-1",
                block_id="block-1",
                char_start=0,
                char_end=len(original),
                extraction_text=original,
            )
        ],
    )


def _generate(
    source: RevisionReviewSource,
    text: str,
) -> tuple[RevisionDraftResponse, _Generator]:
    provider = InMemoryRevisionSourceProvider(
        {(source.review_id, source.generation_id, source.result_hash): source}
    )
    generator = _Generator(text)
    service = RevisionDraftService(
        source_provider=provider,
        generator=generator,
        cache=InMemoryRevisionDraftCache(),
    )
    response = asyncio.run(
        service.get_or_generate(
            source.review_id,
            source.generation_id,
            source.result_hash,
        )
    )
    return response, generator


def test_absence_generates_a_manual_supplement_with_a_certain_placeholder() -> None:
    source = _absence_source()
    response, generator = _generate(
        source,
        "乙方未按约履行义务的，应在某个工作日内采取补救措施，并承担相应责任。",
    )

    assert response.status == "COMPLETED"
    assert response.model_call_count == 1
    assert len(response.drafts) == 1
    draft = response.drafts[0]
    assert draft.operation == "SUPPLEMENT"
    assert draft.original_text is None
    assert draft.target is None
    assert draft.replacement_text is not None
    assert "某个工作日" in draft.replacement_text
    assert len(generator.calls) == 1
    assert isinstance(generator.calls[0][0], SupplementRequest)


def test_absence_supplement_rejects_concrete_duration_and_amount() -> None:
    source = _absence_source()
    response, _ = _generate(source, "乙方应在10个工作日内支付100元。")

    assert response.status == "FAILED"
    assert response.drafts == []
    assert response.failed_findings[0].error_code == "REVISION_GENERATION_FAILED"
    assert "concrete amount" in response.failed_findings[0].message


def test_absence_without_verification_metadata_is_not_generated() -> None:
    source = _absence_source(verification_note=None)
    response, generator = _generate(source, "乙方应承担补救责任。")

    assert response.status == "FAILED"
    assert response.model_call_count == 0
    assert response.failed_findings[0].error_code == "SOURCE_TEXT_INVALID"
    assert generator.calls == []


def test_text_quote_continues_to_generate_a_replace_draft() -> None:
    source = _replace_source()
    response, _ = _generate(source, "乙方应按约完成服务并提交验收材料。")

    assert response.status == "COMPLETED"
    assert response.drafts[0].operation == "REPLACE"
    assert response.drafts[0].target is not None


def test_formal_payload_retains_absence_verification_note() -> None:
    result_hash = "sha256:" + "c" * 64
    source = source_from_formal_payload(
        {
            "review_id": "review-payload-1",
            "result_hash": result_hash,
            "contract_profile": {
                "perspective": "PARTY_A",
                "our_party": "甲方公司",
                "counterparty": "乙方公司",
            },
            "findings": [
                {
                    "finding_id": "finding-payload-1",
                    "title": "缺少约定",
                    "issue": "未见约定。",
                    "suggestion": "补充约定。",
                    "risk_level": "LOW",
                    "evidence_ids": ["evidence-payload-1"],
                }
            ],
            "evidences": [
                {
                    "evidence_id": "evidence-payload-1",
                    "evidence_type": "ABSENCE",
                    "checked_scope": "第七条",
                    "verification_note": "已检查第七条，未见相关安排。",
                }
            ],
        },
        generation_id="generation-payload-1",
        contract_ir=[],
    )

    assert source.evidences[0].verification_note == "已检查第七条，未见相关安排。"
