from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from services.contract.capabilities.revision_drafts import (
    GeneratedReplacement,
    BundledRevisionRequest,
    InMemoryRevisionDraftCache,
    InMemoryRevisionSourceProvider,
    ReplacementBatchResult,
    REVISION_DRAFT_CACHE_VERSION,
    RevisionDraftResponse,
    RevisionDraftService,
    RevisionDocumentBlock,
    RevisionEvidenceSource,
    RevisionFindingSource,
    RevisionGenerationRequest,
    RevisionIrSource,
    RevisionReviewSource,
    SupplementRequest,
    _cache_key,
    compute_revision_hash,
    _find_contract_ir_list,
    _model_request_payload,
    source_from_formal_payload,
)


@dataclass
class _Generator:
    replacement_text: str
    anchor_block_id: str | None = None
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
                    anchor_block_id=(
                        self.anchor_block_id
                        if isinstance(item, SupplementRequest) else None
                    ),
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
        document_blocks=[
            RevisionDocumentBlock(
                block_id="block-remedy-heading",
                block_no=1,
                block_type="paragraph",
                char_start=0,
                char_end=len("section-remedy"),
                text="section-remedy",
                heading_path=["section-remedy"],
            ),
            RevisionDocumentBlock(
                block_id="block-remedy-end",
                block_no=2,
                block_type="paragraph",
                char_start=0,
                char_end=len("Party B bears liability for breach."),
                text="Party B bears liability for breach.",
                heading_path=["section-remedy"],
            ),
            RevisionDocumentBlock(
                block_id="block-dispute-end",
                block_no=3,
                block_type="paragraph",
                char_start=0,
                char_end=len("section-dispute"),
                text="section-dispute",
                heading_path=["section-dispute"],
            ),
        ],
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
    anchor_block_id: str | None = None,
) -> tuple[RevisionDraftResponse, _Generator]:
    provider = InMemoryRevisionSourceProvider(
        {(source.review_id, source.generation_id, source.result_hash): source}
    )
    generator = _Generator(text, anchor_block_id=anchor_block_id)
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
        anchor_block_id="block-remedy-end",
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
    assert draft.insertion_target is not None
    assert draft.insertion_target.block_id == "block-remedy-end"
    assert draft.insertion_target.block_no == 2
    assert draft.insertion_target.display_position == "\u5efa\u8bae\u63d2\u5165\u5230\u201csection-remedy\u201d\u672b\u5c3e"


def test_absence_keeps_text_when_model_returns_an_unknown_anchor() -> None:
    source = _absence_source()
    response, _ = _generate(
        source,
        "Party B must take remedial action after breach.",
        anchor_block_id="invented-block",
    )

    assert response.status == "COMPLETED"
    assert response.drafts[0].replacement_text is not None
    assert response.drafts[0].insertion_target is None
    assert response.drafts[0].revision_group_id is not None
    assert response.drafts[0].numbering_domain_id == "isolated:finding-absence-1"


def test_absence_without_document_blocks_still_generates_text() -> None:
    source = _absence_source().model_copy(update={"document_blocks": []})
    response, generator = _generate(
        source,
        "Party B must take remedial action after breach.",
    )

    assert response.status == "COMPLETED"
    assert response.drafts[0].insertion_target is None
    request = generator.calls[0][0]
    assert isinstance(request, SupplementRequest)
    assert request.insertion_candidates == ()



def test_absence_infers_section_end_and_selects_confident_anchor() -> None:
    source = _absence_source()
    finding = source.findings[0].model_copy(
        update={
            "title": "\u7f3a\u5c11\u7b2c\u4e09\u65b9\u77e5\u8bc6\u4ea7\u6743\u4fdd\u8bc1",
            "issue": "\u5408\u540c\u672a\u7ea6\u5b9a\u7b2c\u4e09\u65b9\u77e5\u8bc6\u4ea7\u6743\u4fdd\u8bc1\u3002",
            "suggestion": "\u8865\u5145\u77e5\u8bc6\u4ea7\u6743\u4fdd\u8bc1\u548c\u4fb5\u6743\u6551\u6d4e\u3002",
        }
    )
    evidence = source.evidences[0].model_copy(
        update={"checked_scope": "\u5168\u6587\u77e5\u8bc6\u4ea7\u6743\u6761\u6b3e"}
    )
    blocks = [
        RevisionDocumentBlock(
            block_id="block-ip-heading",
            block_no=1,
            block_type="paragraph",
            char_start=0,
            char_end=len("\u516d\u3001\u77e5\u8bc6\u4ea7\u6743"),
            text="\u516d\u3001\u77e5\u8bc6\u4ea7\u6743",
        ),
        RevisionDocumentBlock(
            block_id="block-ip-clause",
            block_no=2,
            block_type="paragraph",
            char_start=0,
            char_end=len("\u4e59\u65b9\u4ea4\u4ed8\u6210\u679c\u5f52\u7532\u65b9\u6240\u6709\u3002"),
            text="\u4e59\u65b9\u4ea4\u4ed8\u6210\u679c\u5f52\u7532\u65b9\u6240\u6709\u3002",
        ),
        RevisionDocumentBlock(
            block_id="block-ip-end",
            block_no=3,
            block_type="paragraph",
            char_start=0,
            char_end=len("\u7532\u65b9\u6709\u6743\u4fee\u6539\u548c\u518d\u8bb8\u53ef\u4ea4\u4ed8\u6210\u679c\u3002"),
            text="\u7532\u65b9\u6709\u6743\u4fee\u6539\u548c\u518d\u8bb8\u53ef\u4ea4\u4ed8\u6210\u679c\u3002",
        ),
        RevisionDocumentBlock(
            block_id="block-next-heading",
            block_no=4,
            block_type="paragraph",
            char_start=0,
            char_end=len("\u4e03\u3001\u8fdd\u7ea6\u8d23\u4efb"),
            text="\u4e03\u3001\u8fdd\u7ea6\u8d23\u4efb",
        ),
    ]
    focused = source.model_copy(
        update={"findings": [finding], "evidences": [evidence], "document_blocks": blocks}
    )

    response, _ = _generate(
        focused,
        blocks[2].text + "Party B warrants that all deliverables are non-infringing.",
    )

    target = response.drafts[0].insertion_target
    assert target is not None
    assert target.block_id == "block-ip-end"
    assert target.heading_path == ["\u516d\u3001\u77e5\u8bc6\u4ea7\u6743"]
    assert response.drafts[0].replacement_text == "Party B warrants that all deliverables are non-infringing."


def test_absence_supplement_allows_concrete_duration_amount_and_date() -> None:
    source = _absence_source()
    replacement_text = "乙方应自2026年8月1日起10个工作日内支付100元。"
    response, _ = _generate(source, replacement_text)

    assert response.status == "COMPLETED"
    assert response.failed_findings == []
    assert response.drafts[0].replacement_text == replacement_text


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
    assert response.drafts[0].insertion_target is None
    draft = response.drafts[0]
    expected_hash = compute_revision_hash(
        draft.revision_key,
        draft.operation,
        draft.original_text,
        draft.replacement_text,
        draft.target,
        draft.insertion_target,
        revision_group_id=draft.revision_group_id,
        numbering_domain_id=draft.numbering_domain_id,
        numbering_policy=draft.numbering_policy,
        source_finding_ids=draft.source_finding_ids,
        group_owner_finding_id=draft.group_owner_finding_id,
        group_operation_owner=draft.group_operation_owner,
        operation_order=draft.operation_order,
    )
    assert draft.revision_hash == expected_hash


def test_same_anchor_findings_are_combined_into_one_group_operation() -> None:
    source = _replace_source()
    second = source.findings[0].model_copy(
        update={
            "finding_id": "finding-replace-2",
            "title": "验收标准不完整",
            "issue": "服务验收标准不完整。",
            "suggestion": "补充明确、可验证的验收标准。",
        }
    )
    source = source.model_copy(update={"findings": [source.findings[0], second]})

    response, generator = _generate(source, "乙方应按约完成服务并提交验收材料。")

    assert response.status == "COMPLETED"
    assert response.model_call_count == 2
    assert len(generator.calls) == 2
    assert len(generator.calls[0]) == 2
    assert len(generator.calls[1]) == 1
    bundled_request = generator.calls[1][0]
    assert isinstance(bundled_request, BundledRevisionRequest)
    assert _model_request_payload(bundled_request, source)["request_type"] == "COMBINE_SAME_ANCHOR"
    assert response.drafts[0].revision_group_id == response.drafts[1].revision_group_id
    assert response.drafts[0].source_finding_ids == ["finding-replace-1", "finding-replace-2"]
    assert sum(item.group_operation_owner for item in response.drafts) == 1


def test_literal_list_supplement_uses_append_policy_without_renumbering() -> None:
    source = _absence_source().model_copy(
        update={
            "document_blocks": [
                RevisionDocumentBlock(
                    block_id="block-list-1",
                    block_no=1,
                    block_type="paragraph",
                    char_start=0,
                    char_end=len("1. 乙方应承担违约责任。"),
                    text="1. 乙方应承担违约责任。",
                    heading_path=["违约责任"],
                    metadata={"literal_marker": "1.", "container_path": "document/body"},
                ),
                RevisionDocumentBlock(
                    block_id="block-list-2",
                    block_no=2,
                    block_type="paragraph",
                    char_start=20,
                    char_end=20 + len("2. 甲方有权要求赔偿。"),
                    text="2. 甲方有权要求赔偿。",
                    heading_path=["违约责任"],
                    metadata={"literal_marker": "2.", "container_path": "document/body"},
                ),
            ]
        }
    )

    response, _ = _generate(
        source,
        "乙方违约后应在某个工作日内采取补救措施。",
        anchor_block_id="block-list-2",
    )

    assert response.drafts[0].numbering_policy == "APPEND_LITERAL"
    assert response.drafts[0].numbering_domain_id is not None


def test_same_physical_anchor_across_semantic_ir_items_generates_replace_draft() -> None:
    source = _replace_source()
    primary = source.contract_ir[0]
    source = source.model_copy(
        update={
            "contract_ir": [
                primary,
                primary.model_copy(update={"ir_id": "ir-price", "anchor_id": "anchor-price"}),
                primary.model_copy(update={"ir_id": "ir-obligation", "anchor_id": "anchor-obligation"}),
            ]
        }
    )

    response, generator = _generate(source, "乙方应按约完成服务并提交验收材料。")

    assert response.status == "COMPLETED"
    assert response.failed_findings == []
    assert len(generator.calls) == 1
    assert response.drafts[0].operation == "REPLACE"
    assert response.drafts[0].target is not None
    assert response.drafts[0].target.block_id == "block-1"
    assert response.drafts[0].target.char_start == 0
    assert response.drafts[0].target.char_end == len("乙方应按要求完成服务。")


def test_exact_evidence_anchor_wins_over_enclosing_clause_anchor() -> None:
    source = _replace_source()
    primary = source.contract_ir[0]
    evidence_start = 2
    evidence_text = primary.extraction_text[evidence_start:]
    evidence = source.evidences[0].model_copy(
        update={
            "char_start": evidence_start,
            "char_end": primary.char_end,
            "quoted_text": evidence_text,
            "quoted_text_hash": "sha256:"
            + hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
        }
    )
    semantic = primary.model_copy(
        update={
            "ir_id": "ir-semantic",
            "anchor_id": "anchor-semantic",
            "char_start": evidence_start,
            "char_end": primary.char_end,
            "extraction_text": evidence_text,
        }
    )
    source = source.model_copy(
        update={
            "evidences": [evidence],
            "contract_ir": [
                primary,
                semantic,
                semantic.model_copy(update={"ir_id": "ir-semantic-duplicate"}),
            ]
        }
    )

    response, generator = _generate(source, "Party B shall perform the services as agreed.")

    assert response.status == "COMPLETED"
    assert len(generator.calls) == 1
    assert response.drafts[0].target is not None
    assert response.drafts[0].target.anchor_id == "anchor-semantic"
    assert response.drafts[0].target.char_start == evidence_start
    assert response.drafts[0].target.char_end == primary.char_end


def test_distinct_enclosing_ir_ranges_remain_unsafe_without_exact_evidence_anchor() -> None:
    source = _replace_source()
    primary = source.contract_ir[0]
    evidence_start = 2
    evidence_end = primary.char_end - 1
    evidence_text = primary.extraction_text[evidence_start:evidence_end]
    evidence = source.evidences[0].model_copy(
        update={
            "char_start": evidence_start,
            "char_end": evidence_end,
            "quoted_text": evidence_text,
            "quoted_text_hash": "sha256:"
            + hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
        }
    )
    source = source.model_copy(
        update={
            "evidences": [evidence],
            "contract_ir": [
                primary,
                primary.model_copy(
                    update={
                        "ir_id": "ir-narrower-clause",
                        "anchor_id": "anchor-narrower-clause",
                        "char_start": 1,
                        "extraction_text": primary.extraction_text[1:],
                    }
                ),
            ],
        }
    )

    response, generator = _generate(source, "Party B shall perform the services as agreed.")

    assert response.status == "FAILED"
    assert response.drafts == []
    assert generator.calls == []
    assert response.failed_findings[0].error_code == "FINDING_SOURCE_NOT_UNIQUE"


def test_revision_draft_cache_key_is_versioned_for_anchor_rule_changes() -> None:
    key = _cache_key("review-1", "generation-1", "sha256:" + "d" * 64)

    assert key.split("\0", 1)[0] == REVISION_DRAFT_CACHE_VERSION


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
        document_blocks=[
            {
                "block_id": "block-payload-1",
                "block_no": 1,
                "block_type": "paragraph",
                "char_start": 0,
                "char_end": len("section-seven"),
                "text": "section-seven",
                "heading_path": ["section-seven"],
            }
        ],
    )

    assert source.evidences[0].verification_note == "已检查第七条，未见相关安排。"
    assert source.document_blocks[0].block_id == "block-payload-1"
    assert source.document_blocks[0].block_no == 1
    assert source.document_blocks[0].heading_path == ["section-seven"]


def test_contract_ir_locator_skips_party_records_without_ir_identity() -> None:
    party_anchor = {
        "anchor_id": "anchor-party",
        "block_id": "block-party",
        "char_start": 0,
        "char_end": 7,
    }
    clause_anchor = {
        "anchor_id": "anchor-clause",
        "block_id": "block-clause",
        "char_start": 0,
        "char_end": 12,
    }
    selected = _find_contract_ir_list(
        {
            "parties": [
                {"role": "PARTY_A", "name": "Party A", "source_anchors": [party_anchor]}
            ],
            "clauses": [
                {
                    "clause_id": "clause-1",
                    "text": "Clause text.",
                    "source_anchors": [clause_anchor],
                }
            ],
        }
    )

    assert selected[0]["clause_id"] == "clause-1"
