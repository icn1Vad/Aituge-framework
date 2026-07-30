from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from pydantic import TypeAdapter

from contract.api.models import (
    CreateReviewRequest,
    Finding,
    PartyResolutionCreateRequest,
    Perspective,
    ReviewStatus,
)
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
from contract.internal.models import (
    ContractBlocksToolRequest,
    ContractIrToolRequest,
    ContractWindowPlanToolRequest,
)
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.migrate import run_migrations
from contract.persistence.postgres.repository import ContractRepository
from contract.persistence.postgres.review_state import ReviewStateRepository
from pdf_factory import text_pdf_bytes
from test_runtime_reliability_integration import FakeFrameworkGateway


DATABASE_URL = os.getenv("CONTRACT_TEST_DATABASE_URL", "")
CALLBACK_ADAPTER = TypeAdapter(FrameworkCallback)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_party_resolution_preflight_finishes_after_parse_and_party_callbacks(tmp_path: Path) -> None:
    runtime, gateway, context, upload, _request, tenant_id = _runtime(tmp_path)
    try:
        preflight_request = PartyResolutionCreateRequest(
            contract_version_id=f"version-{uuid.uuid4().hex}",
            schema_version="1.0",
        )
        created = runtime.create_party_resolution(
            upload=upload,
            request=preflight_request,
            context=context,
        )
        assert created.status == ReviewStatus.CREATED
        assert runtime.dispatch_pending_attempts() == 1

        running = runtime.get_party_resolution(created.resolution_id, context=context)
        assert running.status == ReviewStatus.RUNNING
        assert running.framework_attempt_no == 1
        assert running.framework_task_id is not None
        assert running.framework_run_id is not None
        assert gateway.requests[-1].task_idempotency_key == (
            f"contract-party-resolution:{created.resolution_id}:attempt:1"
        )

        repository = ContractRepository(runtime.settings)
        callback_repository = FrameworkCallbackRepository(runtime.settings)
        internal = ContractInternalService(
            repository,
            callback_repository,
            document_processor=runtime.document_processor,
        )
        callbacks = FrameworkCallbackService(callback_repository, internal)
        state = runtime.state_repository.get_state(
            created.resolution_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        task_input = FrameworkTaskInput(
            schema_version="1.0",
            review_id=created.resolution_id,
            attempt_no=1,
            business_task_id=state["business_task_id"],
            contract_version_id=preflight_request.contract_version_id,
            document_id=created.document_id,
            perspective="PARTY_A",
            our_party_name=None,
            execution_mode="PARTY_RESOLUTION",
            contract_type="AUTO",
            review_attitude="NEUTRAL",
        )
        execution = SimpleNamespace(
            review_id=created.resolution_id,
            framework_task_id=running.framework_task_id,
            framework_run_id=running.framework_run_id,
        )
        parse_result = internal.execute_stage(
            _stage_request(execution, task_input, "parse_contract", {})
        )
        callbacks.accept(
            created.resolution_id,
            _stage_callback(execution, 1, "parse_contract", parse_result),
        )
        party_result = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1",
            contract_type="SERVICE",
            party_a={"name": "Acme Company"},
            party_b={"name": "Beta Company"},
            perspective="PARTY_A",
            our_party="Acme Company",
            counterparty="Beta Company",
        )
        callbacks.accept(
            created.resolution_id,
            _stage_callback(execution, 2, "resolve_parties", party_result),
        )
        callbacks.accept(
            created.resolution_id,
            _terminal_callback(execution, 3, "RUN_SUCCEEDED"),
        )

        completed = runtime.get_party_resolution(created.resolution_id, context=context)
        assert completed.status == ReviewStatus.SUCCEEDED
        assert completed.party_a_name == "Acme Company"
        assert completed.party_b_name == "Beta Company"
        assert runtime.state_repository.get_result_json(
            created.resolution_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        ) is None
        with pytest.raises(ContractError) as wrong_resource:
            runtime.get_status(created.resolution_id, context=context)
        assert wrong_resource.value.code == "REVIEW_NOT_FOUND"
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_callback_flow_is_atomic_idempotent_and_terminal_safe(tmp_path: Path) -> None:
    runtime, gateway, context, upload, request, tenant_id = _runtime(tmp_path)
    try:
        created = runtime.create_review(upload=upload, request=request, context=context)
        assert created.status == ReviewStatus.CREATED
        assert runtime.dispatch_pending_attempts() == 1
        created = runtime.get_status(created.review_id, context=context)
        assert created.status == ReviewStatus.RUNNING
        assert created.framework_attempt_no == 1

        repository = ContractRepository(runtime.settings)
        callback_repository = FrameworkCallbackRepository(runtime.settings)
        internal = ContractInternalService(
            repository,
            callback_repository,
            document_processor=runtime.document_processor,
        )
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
        stale_lease = parse_callback.model_copy(
            update={
                "lease_version": 1,
                "callback_id": "callback-stale-lease",
            }
        )
        stale = callbacks.accept(created.review_id, stale_lease)
        assert stale.accepted is False and stale.ignored_reason == "STALE_LEASE"
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
        assert invalid_party.value.code == "PARTY_UNRESOLVED"
        assert invalid_party.value.user_action_required is True

        mismatched_user_party = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1",
            contract_type="SERVICE",
            party_a={"name": "Beta Company"},
            party_b={"name": "Acme Company"},
            perspective="PARTY_B",
            our_party="Acme Company",
            counterparty="Beta Company",
        )
        with pytest.raises(ContractError) as unresolved_party:
            callbacks.accept(
                created.review_id,
                _stage_callback(created, 2, "resolve_parties", mismatched_user_party),
            )
        assert unresolved_party.value.code == "PARTY_UNRESOLVED"
        assert unresolved_party.value.retryable is False
        assert unresolved_party.value.user_action_required is True
        assert unresolved_party.value.details == {
            "perspective": "PARTY_B",
            "candidate_parties": ["Beta Company", "Acme Company"],
            "requested_our_party_name": "Beta Company",
        }

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

        status_after_party_resolution = runtime.get_status(created.review_id, context=context)
        assert status_after_party_resolution.party_resolution is not None
        assert status_after_party_resolution.party_resolution.party_a.name == "Acme Company"
        assert status_after_party_resolution.party_resolution.party_b.name == "Beta Company"
        assert status_after_party_resolution.party_resolution.our_party == "Beta Company"
        assert status_after_party_resolution.party_resolution.counterparty == "Acme Company"
        assert status_after_party_resolution.framework_attempt_no == 1

        contract_ir = internal.get_ir(
            ContractIrToolRequest(
                review_id=created.review_id,
                document_id=created.document_id,
            )
        ).contract_ir
        anchor = contract_ir.model_dump(mode="json")["source_anchors"][0]
        extract_result = ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            semantic_ir={
                "rights": [
                    {
                        "item_id": "right-payment",
                        "subject": "Acme Company",
                        "predicate": "receives payment",
                        "object": "services supplied",
                        "source_anchors": [anchor],
                    }
                ]
            },
        )
        invented_anchor = extract_result.model_dump(mode="json")["semantic_ir"]
        invented_anchor["rights"][0]["source_anchors"][0]["char_end"] = 100_000
        with pytest.raises(ContractError) as invalid_anchor:
            callbacks.accept(
                created.review_id,
                _stage_callback(
                    created,
                    3,
                    "extract_contract_ir",
                    ExtractContractIrStageResult(
                        result_type="CONTRACT_IR_STAGE_V1",
                        semantic_ir=invented_anchor,
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
        persisted_ir = active_generation["contract_ir_json"]
        assert persisted_ir["our_party"] is None
        assert persisted_ir["counterparty"] is None
        assert {item["role"]: item["name"] for item in persisted_ir["parties"]} == {
            "PARTY_A": "Acme Company",
            "PARTY_B": "Beta Company",
        }
        assert persisted_ir["rights"][0]["item_id"] == "right-payment"
        perspective_ir = internal.get_ir(
            ContractIrToolRequest(
                review_id=created.review_id,
                document_id=created.document_id,
            )
        ).contract_ir
        assert perspective_ir.our_party == "Beta Company"
        assert perspective_ir.counterparty == "Acme Company"
        block = repository.list_blocks(active_generation["id"], tenant_id=tenant_id)[0]
        tool_blocks = internal.get_blocks(
            ContractBlocksToolRequest(
                review_id=created.review_id,
                document_id=created.document_id,
                limit=200,
            )
        ).blocks
        assert all(item.char_start == 0 and item.char_end == len(item.text) for item in tool_blocks)
        window_plan = internal.get_window_plan(
            ContractWindowPlanToolRequest(
                review_id=created.review_id,
                document_id=created.document_id,
            )
        )
        assert window_plan.generation_id == active_generation["id"]
        assert window_plan.concurrency == 10
        assert [item.sequence_no for item in window_plan.windows] == list(
            range(1, len(window_plan.windows) + 1)
        )
        assert {item.block_id for item in window_plan.expected_blocks} == {
            item.block_id for item in tool_blocks if item.block_type != "footer"
        }
        assert {
            block_id
            for window in window_plan.windows
            for block_id in window.primary_block_ids
        } == {item.block_id for item in window_plan.expected_blocks}
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

        invalid_stage_evidence = evidence.model_copy(
            update={"char_start": len(block["text"]), "char_end": len(block["text"]) + 10},
        )
        with pytest.raises(ContractError) as invalid_stage_result:
            callbacks.accept(
                created.review_id,
                _stage_callback(
                    created,
                    4,
                    "rights_obligations_review",
                    RightsObligationsStageResult(
                        result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
                        findings=[finding],
                        evidences=[invalid_stage_evidence],
                    ),
                ),
            )
        assert invalid_stage_result.value.code == "EVIDENCE_INVALID"
        assert invalid_stage_result.value.details == {
            "evidence_id": "evidence-payment",
            "block_id": block["block_id"],
            "block_length": len(block["text"]),
        }

        mismatched_finding = finding.model_copy(
            update={
                "finding_id": "finding-mismatched",
                "title": "Mismatched evidence ownership",
                "evidence_ids": [evidence.evidence_id],
            }
        )
        with pytest.raises(ContractError) as mismatched_links:
            callbacks.accept(
                created.review_id,
                _stage_callback(
                    created,
                    4,
                    "rights_obligations_review",
                    RightsObligationsStageResult(
                        result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
                        findings=[finding, mismatched_finding],
                        evidences=[evidence],
                    ),
                ),
            )
        assert mismatched_links.value.code == "EVIDENCE_INVALID"
        assert mismatched_links.value.details == {
            "evidence_id": "evidence-payment",
            "finding_id": "finding-mismatched",
            "evidence_finding_id": "finding-payment",
        }

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
        assert len(result.findings) == 1
        assert len(result.evidences) == 1
        assert result.findings[0].finding_id.startswith("finding-")
        assert result.evidences[0].evidence_id.startswith("evidence-")
        assert result.findings[0].evidence_ids == [result.evidences[0].evidence_id]
        assert result.evidences[0].finding_id == result.findings[0].finding_id
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

        second_context = InternalRequestContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            request_id=f"request-second-{uuid.uuid4().hex}",
            idempotency_key=f"idempotency-second-{uuid.uuid4().hex}",
        )
        second_request = request.model_copy(
            update={
                "business_task_id": f"business-second-{uuid.uuid4().hex}",
                "perspective": Perspective.PARTY_A,
                "our_party_name": "Acme Company",
            }
        )
        second = runtime.create_review(upload=upload, request=second_request, context=second_context)
        assert runtime.dispatch_pending_attempts() == 1
        second = runtime.get_status(second.review_id, context=second_context)
        assert second.document_id == created.document_id
        second_task_input = FrameworkTaskInput(
            schema_version="1.0",
            review_id=second.review_id,
            attempt_no=1,
            business_task_id=second_request.business_task_id,
            contract_version_id=second_request.contract_version_id,
            document_id=second.document_id,
            perspective="PARTY_A",
            our_party_name="Acme Company",
            contract_type="AUTO",
            review_attitude="NEUTRAL",
        )
        second_parse = internal.execute_stage(
            _stage_request(second, second_task_input, "parse_contract", {})
        )
        callbacks.accept(second.review_id, _stage_callback(second, 1, "parse_contract", second_parse))
        second_party = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1",
            contract_type="SERVICE",
            party_a={"name": "Acme Company"},
            party_b={"name": "Beta Company"},
            perspective="PARTY_A",
            our_party="Acme Company",
            counterparty="Beta Company",
        )
        callbacks.accept(second.review_id, _stage_callback(second, 2, "resolve_parties", second_party))
        second_extract = ExtractContractIrStageResult(
            result_type="CONTRACT_IR_STAGE_V1",
            semantic_ir={},
        )
        callbacks.accept(
            second.review_id,
            _stage_callback(second, 3, "extract_contract_ir", second_extract),
        )
        reused_generation = repository.get_active_generation(second.document_id, tenant_id=tenant_id)
        assert reused_generation is not None
        assert reused_generation["ir_hash"] == active_generation["ir_hash"]
        assert reused_generation["contract_ir_json"]["our_party"] is None
        second_ir = internal.get_ir(
            ContractIrToolRequest(review_id=second.review_id, document_id=second.document_id)
        ).contract_ir
        assert second_ir.our_party == "Acme Company"
        assert second_ir.counterparty == "Beta Company"
        assert len(gateway.executions) == 2
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
            "lease_version": 2,
            "event_sequence": sequence,
            "callback_id": f"callback-{created.review_id}-lease-2-{sequence}",
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
            "callback_id": f"callback-{created.review_id}-lease-2-{sequence}",
            "lease_version": 2,
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
