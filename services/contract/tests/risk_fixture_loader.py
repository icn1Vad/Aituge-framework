from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from contract.callback.models import ExtractContractIrStageResult, PartyResolutionStageResult
from contract.internal.models import ContractWindowData
from contract.risk.models import RiskReviewPlanInput, RiskSourceBlock


EXPECTED_HASHES = {
    "contract-ir-stage-result-service-outsourcing-0829-v1.json": (
        "bfc3388b471c32938dd8f7092d9c404e4817fa4ade3aff4c951042b9d25ab16c"
    ),
    "contract-risk-review-fixture-service-outsourcing-0829-v1.json": (
        "9a89bde1e4b05e403a6fefe24a50f494feea5af40e7b1d40f4da5fe9b42dc15e"
    ),
    "risk-review-context-service-outsourcing-0829-v1.json": (
        "a76b7059ca41aa4bc3e9c1e0b791dd505138872eb73abc5275d847d6fdecb629"
    ),
}


class StrictFixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixtureSourceDocument(StrictFixtureModel):
    filename: str = Field(min_length=1)
    file_type: str = Field(pattern=r"^(pdf|docx)$")
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    block_count: int = Field(gt=0)
    section_count: int = Field(gt=0)
    window_count: int = Field(gt=0)


class FixtureTaskInput(StrictFixtureModel):
    schema_version: str = Field(pattern=r"^1\.0$")
    review_id: str
    attempt_no: int = Field(ge=1, le=2)
    business_task_id: str
    contract_version_id: str
    document_id: str
    perspective: str
    our_party_name: str
    contract_type: str
    review_attitude: str


class FixtureRiskContext(StrictFixtureModel):
    task_input_template: FixtureTaskInput
    resolve_parties_artifact: PartyResolutionStageResult


def load_fixed_risk_plan_input(directory: Path) -> RiskReviewPlanInput:
    fixture_dir = directory.expanduser().resolve(strict=True)
    if not fixture_dir.is_dir():
        raise ValueError("CONTRACT_RISK_FIXTURE_DIR must point to a readable directory")
    for name, expected in EXPECTED_HASHES.items():
        actual = hashlib.sha256((fixture_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Fixture SHA-256 mismatch: {name}")

    stage_result = ExtractContractIrStageResult.model_validate_json(
        (fixture_dir / "contract-ir-stage-result-service-outsourcing-0829-v1.json").read_text(
            "utf-8"
        )
    )
    fixture_raw = json.loads(
        (fixture_dir / "contract-risk-review-fixture-service-outsourcing-0829-v1.json").read_text(
            "utf-8"
        )
    )
    fixture_stage_result = ExtractContractIrStageResult.model_validate(fixture_raw["stage_result"])
    if fixture_stage_result != stage_result:
        raise ValueError("Standard Stage Artifact differs from the complete Fixture")
    source_document = FixtureSourceDocument.model_validate(fixture_raw["source_document"])
    windows = [ContractWindowData.model_validate(value) for value in fixture_raw["source_windows"]]
    if len(windows) != source_document.window_count:
        raise ValueError("Fixture Window count does not match source_document")

    blocks: dict[str, RiskSourceBlock] = {}
    for window in windows:
        for offset in window.offset_map:
            if offset.block_char_start != 0:
                raise ValueError("Fixed Fixture contains a partial Block segment")
            text = window.source_text[offset.rendered_start : offset.rendered_end]
            if len(text) != offset.block_char_end:
                raise ValueError("Fixture Offset does not cover the complete Block text")
            value = RiskSourceBlock(
                block_id=offset.block_id,
                block_no=offset.block_no,
                text=text,
                page_number=offset.page_number,
                heading_path=window.heading_path,
            )
            previous = blocks.get(value.block_id)
            if previous is not None and previous != value:
                raise ValueError("The same Fixture Block resolves to different text")
            blocks[value.block_id] = value
    if len(blocks) != source_document.block_count:
        raise ValueError("Fixture Block count does not match source_document")

    context = FixtureRiskContext.model_validate_json(
        (fixture_dir / "risk-review-context-service-outsourcing-0829-v1.json").read_text("utf-8")
    )
    party = context.resolve_parties_artifact
    if context.task_input_template.document_id != source_document.document_id:
        raise ValueError("Risk context document_id differs from the fixed Fixture")
    return RiskReviewPlanInput(
        review_id="risk-fixture-review-service-outsourcing-0829-v1",
        document_id=source_document.document_id,
        generation_id=source_document.generation_id,
        attempt_no=1,
        perspective=party.perspective,
        our_party=party.our_party,
        counterparty=party.counterparty,
        contract_type=party.contract_type,
        review_attitude="NEUTRAL",
        stage_result=stage_result,
        source_blocks=sorted(blocks.values(), key=lambda item: item.block_no),
        selected_playbook_ids=["base_neutral"],
    )
