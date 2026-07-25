from __future__ import annotations

from contract.callback.models import ExtractContractIrStageResult
from contract.risk.models import RiskReviewPlanInput, RiskSourceBlock


def risk_plan_input() -> RiskReviewPlanInput:
    text = "甲方应于验收后十日内支付100万元，乙方应交付系统并承担保密义务。"
    anchor = {
        "anchor_id": "anchor-1",
        "block_id": "block-1",
        "page_number": None,
        "char_start": 0,
        "char_end": len(text),
    }
    item_types = (
        "rights",
        "obligations",
        "prohibitions",
        "payment_terms",
        "delivery_terms",
        "acceptance_terms",
        "liabilities",
        "termination_terms",
        "confidentiality_terms",
        "intellectual_property_terms",
        "dispute_resolution",
        "dates",
        "amounts",
    )
    semantic_ir = {
        ir_type: [
            {
                "item_id": f"item-{ir_type}",
                "subject": "甲方" if ir_type in {"rights", "payment_terms"} else "乙方",
                "predicate": "应履行",
                "object": ir_type,
                "source_anchors": [anchor],
            }
        ]
        for ir_type in item_types
    }
    semantic_ir["definitions"] = [
        {
            "term": "系统",
            "meaning": "本合同约定交付的软件系统",
            "source_anchors": [anchor],
        }
    ]
    return RiskReviewPlanInput(
        review_id="review-1",
        document_id="document-1",
        generation_id="generation-1",
        attempt_no=1,
        perspective="PARTY_A",
        our_party="甲方单位",
        counterparty="乙方单位",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        stage_result=ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            semantic_ir=semantic_ir,
        ),
        source_blocks=[
            RiskSourceBlock(
                block_id="block-1",
                block_no=1,
                text=text,
            )
        ],
    )
