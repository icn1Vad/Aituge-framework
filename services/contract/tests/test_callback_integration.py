from __future__ import annotations

import copy
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import TypeAdapter

from contract.api.models import CreateReviewRequest, Finding, Perspective, ReviewStatus
from contract.application.document_processing import ContractDocumentProcessor
from contract.application.ports import InternalRequestContext, UploadedContract
from contract.application.runtime_service import RuntimeContractReviewService
from contract.callback.models import (
    CommercialTermsStageResult,
    EvidenceCandidate,
    ExtractContractIrStageResult,
    FrameworkCallback,
    FrameworkTaskInput,
    LiabilityTerminationStageResult,
    MissingAmbiguityStageResult,
    PartyResolutionStageResult,
    RelationExtractionStageResult,
    RightsObligationsStageResult,
    StageExecuteRequest,
)
from contract.callback.service import FrameworkCallbackService
from contract.config import Settings
from contract.errors import ContractError
from contract.internal.service import ContractInternalService
from contract.internal.models import ContractIrToolRequest
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.migrate import run_migrations
from contract.persistence.postgres.repository import ContractRepository
from contract.persistence.postgres.review_state import ReviewStateRepository
from pdf_factory import text_pdf_bytes
from test_runtime_reliability_integration import FakeFrameworkGateway


DATABASE_URL = os.getenv("CONTRACT_TEST_DATABASE_URL", "")
CALLBACK_ADAPTER = TypeAdapter(FrameworkCallback)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_callback_flow_is_atomic_idempotent_and_terminal_safe(tmp_path: Path) -> None:
    runtime, gateway, context, upload, request, tenant_id = _runtime(tmp_path)
    try:
        created = runtime.create_review(upload=upload, request=request, context=context)
        assert created.status == ReviewStatus.RUNNING
        assert created.framework_attempt_no == 1

        repository = ContractRepository(runtime.settings)
        callback_repository = FrameworkCallbackRepository(runtime.settings)
        internal = ContractInternalService(repository, callback_repository)
        callbacks = FrameworkCallbackService(callback_repository, internal)
        task_input = FrameworkTaskInput(
            schema_version="1.0",
            review_id=created.review_id,
            attempt_no=1,
            business_task_id=request.business_task_id,
            contract_version_id=request.contract_version_id,
            document_id=created.document_id,
            perspective=request.perspective.value,
            our_party_name=request.our_party_name,
            contract_type="AUTO",
            review_attitude="NEUTRAL",
        )

        artifacts: dict[str, dict[str, Any]] = {}
        parse_result = internal.execute_stage(
            _stage_request(created, task_input, "parse_contract", artifacts)
        )
        parse_callback = _stage_callback(created, 1, "parse_contract", parse_result)
        first = callbacks.accept(created.review_id, parse_callback)
        repeated = callbacks.accept(created.review_id, parse_callback)
        assert first.accepted is True and first.duplicate is False
        assert repeated.accepted is True and repeated.duplicate is True

        changed_replay = parse_callback.model_copy(
            update={"event_sequence": 999},
        )
        with pytest.raises(ContractError, match="callback_id was reused"):
            callbacks.accept(created.review_id, changed_replay)

        invented_party_result = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1",
            contract_type="SERVICE",
            party_a={"name": "Invented Party A"},
            party_b={"name": "Invented Party B"},
            perspective="PARTY_B",
            our_party="Invented Party B",
            counterparty="Invented Party A",
        )
        with pytest.raises(ContractError) as invalid_party:
            callbacks.accept(
                created.review_id,
                _stage_callback(created, 2, "resolve_parties", invented_party_result),
            )
        assert invalid_party.value.code == "RESULT_INVALID"

        party_result = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1",
            contract_type="SERVICE",
            party_a={"name": "Acme Company"},
            party_b={"name": "Beta Company"},
            perspective="PARTY_B",
            our_party="Beta Company",
            counterparty="Acme Company",
        )
        artifacts["contract_party_resolution"] = party_result.model_dump(mode="json")
        callbacks.accept(
            created.review_id,
            _stage_callback(created, 2, "resolve_parties", party_result),
        )

        contract_ir = internal.get_ir(
            ContractIrToolRequest(
                review_id=created.review_id,
                document_id=created.document_id,
            )
        ).contract_ir
        contract_ir_value = contract_ir.model_dump(mode="json")
        anchor = contract_ir_value["source_anchors"][0]
        contract_ir_value.update(
            {
                "parties": [
                    {"role": "PARTY_A", "name": "Acme Company", "source_anchors": [anchor]},
                    {"role": "PARTY_B", "name": "Beta Company", "source_anchors": [anchor]},
                ],
                "our_party": "Beta Company",
                "counterparty": "Acme Company",
                "contract_type": "SERVICE",
            }
        )
        extract_result = ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            contract_ir=contract_ir_value,
        )
        changed_structure = copy.deepcopy(contract_ir_value)
        changed_structure["clauses"][0]["text"] += " invented"
        with pytest.raises(ContractError) as invalid_structure:
            callbacks.accept(
                created.review_id,
                _stage_callback(
                    created,
                    3,
                    "extract_contract_ir",
                    ExtractContractIrStageResult(
                        result_type="CONTRACT_IR_STAGE_V1",
                        contract_ir=changed_structure,
                    ),
                ),
            )
        assert invalid_structure.value.code == "RESULT_INVALID"

        invented_anchor = copy.deepcopy(contract_ir_value)
        invented_anchor["rights"] = [
            {
                "item_id": "right-invented",
                "subject": "Beta Company",
                "predicate": "has an invented source range",
                "object": None,
                "source_anchors": [{**anchor, "char_end": 100_000}],
            }
        ]
        with pytest.raises(ContractError) as invalid_anchor:
            callbacks.accept(
                created.review_id,
                _stage_callback(
                    created,
                    3,
                    "extract_contract_ir",
                    ExtractContractIrStageResult(
                        result_type="CONTRACT_IR_STAGE_V1",
                        contract_ir=invented_anchor,
                    ),
                ),
            )
        assert invalid_anchor.value.code == "RESULT_INVALID"

        artifacts["contract_ir"] = extract_result.model_dump(mode="json")
        callbacks.accept(
            created.review_id,
            _stage_callback(created, 3, "extract_contract_ir", extract_result),
        )
        active_generation = repository.get_active_generation(created.document_id, tenant_id=tenant_id)
        assert active_generation is not None
        block = repository.list_blocks(active_generation["id"], tenant_id=tenant_id)[0]
        quoted_text = "Party B pays."
        char_start = block["text"].index(quoted_text)
        evidence = EvidenceCandidate(
            evidence_id="evidence-payment",
            finding_id="finding-payment",
            evidence_type="TEXT_QUOTE",
            block_id=block["block_id"],
            page_number=block["page_number"],
            char_start=char_start,
            char_end=char_start + len(quoted_text),
        )
        finding = Finding(
            finding_id="finding-payment",
            category="PAYMENT",
            risk_level="MEDIUM",
            title="Payment protection is insufficient",
            perspective="PARTY_B",
            our_party="Beta Company",
            counterparty="Acme Company",
            issue="The payment obligation lacks detailed protection.",
            impact_to_our_party="Beta Company may face delayed payment.",
            suggestion="Add a payment deadline and late-payment consequences.",
            evidence_ids=[evidence.evidence_id],
        )

        review_stages = [
            (
                "rights_obligations_review",
                "rights_obligations_review_result",
                RightsObligationsStageResult(
                    result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
                    findings=[finding],
                    evidences=[evidence],
                ),
            ),
            (
                "commercial_terms_review",
                "commercial_terms_review_result",
                CommercialTermsStageResult(result_type="COMMERCIAL_TERMS_STAGE_V1"),
            ),
            (
                "liability_termination_review",
                "liability_termination_review_result",
                LiabilityTerminationStageResult(result_type="LIABILITY_TERMINATION_STAGE_V1"),
            ),
            (
                "missing_ambiguous_clauses",
                "missing_ambiguous_clauses_result",
                MissingAmbiguityStageResult(result_type="MISSING_AMBIGUITY_STAGE_V1"),
            ),
            (
                "relation_extraction",
                "relation_extraction_result",
                RelationExtractionStageResult(result_type="RELATION_EXTRACTION_STAGE_V1"),
            ),
        ]
        for sequence, (stage_id, artifact_type, result) in enumerate(review_stages, start=4):
            artifacts[artifact_type] = result.model_dump(mode="json")
            callbacks.accept(
                created.review_id,
                _stage_callback(created, sequence, stage_id, result),
            )

        verified = internal.execute_stage(
            _stage_request(created, task_input, "verify_evidence", artifacts)
        )
        artifacts["contract_verified_findings"] = verified.model_dump(mode="json")
        callbacks.accept(
            created.review_id,
            _stage_callback(created, 9, "verify_evidence", verified),
        )

        run_succeeded = _terminal_callback(created, 100, "RUN_SUCCEEDED")
        callbacks.accept(created.review_id, run_succeeded)
        assert runtime.get_status(created.review_id, context=context).status == ReviewStatus.RUNNING

        finalized = internal.execute_stage(
            _stage_request(created, task_input, "finalize_review", artifacts)
        )
        callbacks.accept(
            created.review_id,
            _stage_callback(created, 10, "finalize_review", finalized),
        )

        status = runtime.get_status(created.review_id, context=context)
        assert status.status == ReviewStatus.SUCCEEDED
        result = runtime.get_result(created.review_id, context=context)
        assert result.review_id == created.review_id
        assert result.result_hash.startswith("sha256:")
        assert [item.finding_id for item in result.findings] == ["finding-payment"]
        assert [item.evidence_id for item in result.evidences] == ["evidence-payment"]
        assert result.summary.medium_count == 1

        conflict = callbacks.accept(
            created.review_id,
            _terminal_callback(
                created,
                101,
                "RUN_FAILED",
                error={
                    "code": "LATE_FAILURE",
                    "message": "Late opposite terminal callback",
                    "retryable": False,
                    "user_action_required": False,
                    "details": None,
                },
            ),
        )
        assert conflict.accepted is False
        assert conflict.ignored_reason == "FRAMEWORK_PROTOCOL_ERROR"
        assert runtime.get_status(created.review_id, context=context).status == ReviewStatus.SUCCEEDED

        with psycopg.connect(DATABASE_URL) as conn:
            rows = conn.execute(
                """
                SELECT callback_type, validation_status, ignored_reason
                FROM contract_review_stage_result
                WHERE review_id = %s
                ORDER BY event_sequence
                """,
                (created.review_id,),
            ).fetchall()
            result_count = conn.execute(
                "SELECT COUNT(*) FROM contract_review_result WHERE review_id = %s",
                (created.review_id,),
            ).fetchone()[0]
        assert len(rows) == 12
        assert rows[-1] == ("RUN_FAILED", "REJECTED", "FRAMEWORK_PROTOCOL_ERROR")
        assert result_count == 1
        assert len(gateway.executions) == 1
    finally:
        _cleanup(tenant_id)


def _runtime(tmp_path: Path):
    run_migrations(Settings(database_url=DATABASE_URL))
    marker = uuid.uuid4().hex
    tenant_id = f"tenant-{marker}"
    settings = Settings(database_url=DATABASE_URL, data_dir=tmp_path)
    gateway = FakeFrameworkGateway()
    repository = ContractRepository(settings)
    runtime = RuntimeContractReviewService(
        settings,
        repository,
        ReviewStateRepository(settings),
        ContractDocumentProcessor(settings, repository),
        gateway,
    )
    context = InternalRequestContext(
        tenant_id=tenant_id,
        user_id=f"user-{marker}",
        request_id=f"request-{marker}",
        idempotency_key=f"idempotency-{marker}",
    )
    request = CreateReviewRequest(
        business_task_id=f"business-{marker}",
        contract_version_id=f"version-{marker}",
        perspective=Perspective.PARTY_B,
        our_party_name="Beta Company",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        schema_version="1.0",
    )
    upload = UploadedContract(
        filename="contract.pdf",
        content_type="application/pdf",
        content=text_pdf_bytes(
            "Party A: Acme Company; Party B: Beta Company. Party A supplies services. Party B pays."
        ),
    )
    return runtime, gateway, context, upload, request, tenant_id


def _stage_request(created, task_input, stage_id, artifacts):
    return StageExecuteRequest(
        schema_version="1.0",
        review_id=created.review_id,
        attempt_no=1,
        framework_task_id=created.framework_task_id,
        framework_run_id=created.framework_run_id,
        stage_id=stage_id,
        task_input=task_input,
        artifacts=artifacts,
    )


def _stage_callback(created, sequence: int, stage_id: str, result):
    value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return CALLBACK_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "review_id": created.review_id,
            "attempt_no": 1,
            "framework_task_id": created.framework_task_id,
            "framework_run_id": created.framework_run_id,
            "event_sequence": sequence,
            "callback_id": f"callback-{sequence}",
            "callback_type": "STAGE_RESULT",
            "stage_id": stage_id,
            "result": value,
            "error": None,
        }
    )


def _terminal_callback(created, sequence: int, callback_type: str, *, error=None):
    return CALLBACK_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "review_id": created.review_id,
            "attempt_no": 1,
            "framework_task_id": created.framework_task_id,
            "framework_run_id": created.framework_run_id,
            "event_sequence": sequence,
            "callback_id": f"callback-{sequence}",
            "callback_type": callback_type,
            "stage_id": None,
            "result": None,
            "error": error,
        }
    )


def _cleanup(tenant_id: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM contract_review_result WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM contract_review_run WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM contract_document WHERE tenant_id = %s", (tenant_id,))
        conn.commit()
