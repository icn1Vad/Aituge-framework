"""Internal Direct risk-review end-to-end dry-run support.

This module deliberately reuses the frozen formal result and callback models.
It never opens a database connection, invokes the Java callback endpoint, or
changes the production pipeline.
"""

from __future__ import annotations

import copy
import hashlib
import time
from collections import Counter
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contract.api.models import LegalEvidenceReference, ReviewResultData, ReviewSummary
from contract.application.idempotency import canonical_json
from contract.application.result_hash import compute_result_hash
from contract.callback.models import (
    EvidenceVerificationStageResult,
    FinalizeReviewStageResult,
    StageResultCallback,
)
from contract.review import namespace_review_stage_result
from services.contract.capabilities.legacy_compatibility import (
    LegacyCompatibleRiskReviewResult,
    LegacyCompatibilityContext,
)


# Frozen in both FrameworkHttpGateway.STAGE_MAPPING and
# FrameworkCallbackRepository.STAGE_TO_REVIEW_STAGE. Keeping this tiny local
# assertion table avoids importing database/runtime configuration into the
# isolated dry-run validator.
_FORMAL_STAGE_MAPPING = {
    "verify_evidence": "EVIDENCE_VERIFICATION",
    "finalize_review": "FINALIZING",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectE2EExecutionMode(str, Enum):
    DRY_RUN = "DRY_RUN"


class DirectE2EStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DryRunSinkStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


class DryRunCallbackStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


class DirectRiskReviewEndToEndRequest(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_id: str = Field(min_length=1, max_length=160)
    execution_mode: Literal["DRY_RUN"] = "DRY_RUN"
    contract_ir_stage_result: dict[str, Any]
    risk_review_context: dict[str, Any]


class DirectE2EStageMetric(StrictModel):
    stage: str = Field(min_length=1, max_length=160)
    wall_ms: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    repair_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class DirectE2EStateEvent(StrictModel):
    sequence: int = Field(ge=1)
    event: str = Field(min_length=1, max_length=160)
    review_status: Literal["RUNNING", "SUCCEEDED", "FAILED"]
    formal_stage: Literal[
        "RISK_REVIEW",
        "EVIDENCE_VERIFICATION",
        "FINALIZING",
    ]


class DryRunSinkReceipt(StrictModel):
    payload_received: bool
    schema_valid: bool
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=r"^result-[0-9a-f]{32}$")
    status: DryRunSinkStatus
    would_insert: bool
    would_update: bool
    would_callback: bool
    transaction_status: Literal["COMMITTED", "ROLLED_BACK"]
    write_effect: Literal["NONE"] = "NONE"


class DryRunCallbackReceipt(StrictModel):
    callback_id: str = Field(min_length=1, max_length=200)
    status: DryRunCallbackStatus
    accepted: bool
    duplicate: bool
    would_send: bool
    callback_effect: Literal["NONE"] = "NONE"


class DirectRiskReviewEndToEndResult(StrictModel):
    run_id: str = Field(min_length=1, max_length=160)
    review_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_id: str = Field(min_length=1, max_length=160)
    status: DirectE2EStatus
    extended_bundle_status: Literal["SUCCEEDED"]
    compatibility_status: Literal["SUCCEEDED"]
    semantic_merge_status: Literal["COMPLETED", "SKIPPED"]
    evidence_verification_status: Literal["VERIFIED"]
    payload_validation_status: Literal["VALID"]
    dry_run_sink_status: DryRunSinkStatus
    dry_run_callback_status: DryRunCallbackStatus
    formal_result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    formal_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    core_result_signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stage_metrics: list[DirectE2EStageMetric]
    state_transition_trace: list[DirectE2EStateEvent]
    sink_receipt: DryRunSinkReceipt
    callback_receipt: DryRunCallbackReceipt
    write_effect: Literal["NONE"] = "NONE"
    callback_effect: Literal["NONE"] = "NONE"
    pipeline_cutover_effect: Literal["NONE"] = "NONE"
    legacy_model_calls: Literal[0] = 0
    legacy_tool_calls: Literal[0] = 0

    @model_validator(mode="after")
    def validate_success(self) -> "DirectRiskReviewEndToEndResult":
        if self.status == DirectE2EStatus.SUCCEEDED:
            if self.dry_run_sink_status not in {
                DryRunSinkStatus.ACCEPTED,
                DryRunSinkStatus.DUPLICATE,
            }:
                raise ValueError("Successful E2E requires a successful dry-run sink")
            if self.dry_run_callback_status not in {
                DryRunCallbackStatus.ACCEPTED,
                DryRunCallbackStatus.DUPLICATE,
            }:
                raise ValueError("Successful E2E requires a successful dry-run callback")
        return self


class DirectE2EError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def stable_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def frozen_input_snapshot(
    contract_ir_stage_result: Mapping[str, Any],
    risk_review_context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    left = copy.deepcopy(dict(contract_ir_stage_result))
    right = copy.deepcopy(dict(risk_review_context))
    return left, right, stable_hash({"contract_ir": left, "context": right})


def build_formal_result(
    compatible: LegacyCompatibleRiskReviewResult,
    *,
    context: LegacyCompatibilityContext,
    generation_id: str,
    framework_task_id: str,
    framework_run_id: str,
    legal_evidence_bundle: Any | None = None,
) -> tuple[FinalizeReviewStageResult, ReviewResultData, str]:
    """Build and validate the exact formal final-stage/result DTOs."""

    overview = (
        f"发现{len(compatible.final_findings)}项需要人工复核的合同事项。"
        if compatible.final_findings
        else "未发现需要人工复核的实质合同风险。"
    )
    verified = EvidenceVerificationStageResult(
        result_type="EVIDENCE_VERIFICATION_STAGE_V1",
        contract_profile=context.contract_profile,
        overview=overview,
        findings=compatible.final_findings,
        evidences=compatible.final_evidence,
        relationships=[],
    )
    counts = Counter(finding.risk_level.value for finding in verified.findings)
    referenced_legal_ids = {
        evidence_id
        for finding in verified.findings
        for evidence_id in finding.legal_evidence_ids
    }
    legal_catalog: list[LegalEvidenceReference] = []
    legal_release_id = None
    legal_bundle_hash = None
    if referenced_legal_ids:
        if legal_evidence_bundle is None or not legal_evidence_bundle.usable:
            raise DirectE2EError(
                "LEGAL_EVIDENCE_REFERENCE_INVALID",
                "Final findings reference legal evidence without a usable frozen bundle",
            )
        evidence_by_id = {
            item.evidence_id: item
            for item in legal_evidence_bundle.evidence
            if item.check_codes
        }
        unknown = sorted(referenced_legal_ids - set(evidence_by_id))
        if unknown:
            raise DirectE2EError(
                "LEGAL_EVIDENCE_REFERENCE_INVALID",
                "Final findings reference evidence outside the frozen bundle: "
                + ",".join(unknown),
            )
        if not legal_evidence_bundle.release_id:
            raise DirectE2EError(
                "LEGAL_EVIDENCE_REFERENCE_INVALID",
                "Cited legal evidence bundle has no release identity",
            )
        legal_release_id = legal_evidence_bundle.release_id
        legal_bundle_hash = legal_evidence_bundle.bundle_hash
        for evidence_id in sorted(referenced_legal_ids):
            evidence = evidence_by_id[evidence_id]
            unit = evidence.unit
            legal_catalog.append(
                LegalEvidenceReference(
                    evidence_id=evidence.evidence_id,
                    release_id=unit.release_id,
                    unit_id=unit.unit_id,
                    source_node_ids=unit.source_node_ids,
                    title=unit.title,
                    article_no=unit.article_no,
                    heading_path=unit.heading_path,
                    content=unit.content,
                    jurisdiction=unit.jurisdiction,
                    authority_level=unit.authority_level,
                    issuing_authority=unit.issuing_authority,
                    effective_from=unit.effective_from,
                    effective_to=unit.effective_to,
                    validity_status=unit.validity_status,
                    metadata_verification_status=unit.metadata_verification_status,
                    official_source_url=unit.official_source_url,
                    content_hash=unit.content_hash,
                    check_codes=evidence.check_codes,
                    issue_ids=evidence.issue_ids,
                    cautions=evidence.cautions,
                )
            )

    formal = FinalizeReviewStageResult(
        result_type="FINAL_REVIEW_STAGE_V1",
        schema_version="1.0",
        review_id=context.review_id,
        business_task_id=context.business_task_id,
        contract_version_id=context.contract_version_id,
        contract_profile=verified.contract_profile,
        summary=ReviewSummary(
            overview=verified.overview,
            high_count=counts["HIGH"],
            medium_count=counts["MEDIUM"],
            low_count=counts["LOW"],
            info_count=counts["INFO"],
        ),
        findings=verified.findings,
        evidences=verified.evidences,
        legal_evidence_release_id=legal_release_id,
        legal_evidence_bundle_hash=legal_bundle_hash,
        legal_evidences=legal_catalog,
        relationships=[],
    )
    raw = formal.model_dump(mode="json")
    raw.pop("result_type")
    result_hash, _ = compute_result_hash(raw)
    result = ReviewResultData.model_validate({**raw, "result_hash": result_hash})
    return formal, result, stable_hash(formal.model_dump(mode="json"))


def core_result_signature(
    compatible: LegacyCompatibleRiskReviewResult,
) -> str:
    routing = routing_by_final_finding_id(compatible)
    core = []
    for finding in compatible.final_findings:
        route = routing.get(finding.finding_id)
        if route is None:
            raise DirectE2EError(
                "RISK_E2E_ROUTING_TRACE_MISSING",
                f"Missing routing trace for {finding.finding_id}",
            )
        core.append(
            {
                "check_code": route.source_check_code,
                "risk_type": route.risk_type,
                "risk_level": finding.risk_level.value,
                # Evidence IDs are the frozen source identity. Explanatory
                # checked_scope/verification_note wording is intentionally not
                # part of the core signature.
                "primary_evidence_ids": sorted(finding.evidence_ids),
            }
        )
    return stable_hash(sorted(core, key=lambda value: canonical_json(value)))


def routing_by_final_finding_id(
    compatible: LegacyCompatibleRiskReviewResult,
) -> dict[str, Any]:
    routing_by_compatible = {
        item.compatible_finding_id: item for item in compatible.routing_records
    }
    routing: dict[str, Any] = {}
    for artifact_type in compatible.legacy_artifacts.as_artifact_dict():
        source = getattr(compatible.legacy_artifacts, artifact_type)
        namespaced = namespace_review_stage_result(artifact_type, source)
        for before, after in zip(source.findings, namespaced.findings, strict=True):
            route = routing_by_compatible.get(before.finding_id)
            if route is not None:
                routing[after.finding_id] = route
    return routing


class DryRunResultSink:
    """In-memory simulation of the frozen one-result-per-review transaction."""

    def __init__(self) -> None:
        self._results: dict[str, ReviewResultData] = {}
        self._callbacks: dict[str, dict[str, Any]] = {}

    @staticmethod
    def idempotency_key(review_id: str, result_hash: str) -> str:
        digest = hashlib.sha256(f"{review_id}\0{result_hash}".encode("utf-8")).hexdigest()
        return f"result-{digest[:32]}"

    def submit(
        self,
        payload: Mapping[str, Any],
        *,
        fail_transaction: bool = False,
    ) -> DryRunSinkReceipt:
        value = ReviewResultData.model_validate(payload)
        calculated, _ = compute_result_hash(
            {
                key: item
                for key, item in value.model_dump(mode="json").items()
                if key != "result_hash"
            }
        )
        if calculated != value.result_hash:
            raise DirectE2EError(
                "RISK_FORMAL_RESULT_HASH_INVALID",
                "Formal result_hash does not match the frozen hash algorithm",
            )
        key = self.idempotency_key(value.review_id, value.result_hash)
        if fail_transaction:
            return DryRunSinkReceipt(
                payload_received=True,
                schema_valid=True,
                result_hash=value.result_hash,
                idempotency_key=key,
                status=DryRunSinkStatus.FAILED,
                would_insert=False,
                would_update=False,
                would_callback=False,
                transaction_status="ROLLED_BACK",
            )
        existing = self._results.get(value.review_id)
        if existing is not None:
            if existing.result_hash != value.result_hash:
                raise DirectE2EError(
                    "FRAMEWORK_CALLBACK_MISMATCH",
                    "Review result was already finalized with a different hash",
                )
            return DryRunSinkReceipt(
                payload_received=True,
                schema_valid=True,
                result_hash=value.result_hash,
                idempotency_key=key,
                status=DryRunSinkStatus.DUPLICATE,
                would_insert=False,
                would_update=False,
                would_callback=False,
                transaction_status="COMMITTED",
            )
        self._results[value.review_id] = value.model_copy(deep=True)
        return DryRunSinkReceipt(
            payload_received=True,
            schema_valid=True,
            result_hash=value.result_hash,
            idempotency_key=key,
            status=DryRunSinkStatus.ACCEPTED,
            would_insert=True,
            would_update=False,
            would_callback=True,
            transaction_status="COMMITTED",
        )

    def callback(
        self,
        callback: StageResultCallback,
        *,
        fail_callback: bool = False,
    ) -> DryRunCallbackReceipt:
        payload = callback.model_dump(mode="json")
        if fail_callback:
            return DryRunCallbackReceipt(
                callback_id=callback.callback_id,
                status=DryRunCallbackStatus.FAILED,
                accepted=False,
                duplicate=False,
                would_send=True,
            )
        previous = self._callbacks.get(callback.callback_id)
        if previous is not None:
            if previous != payload:
                raise DirectE2EError(
                    "FRAMEWORK_CALLBACK_MISMATCH",
                    "callback_id was reused with a different callback envelope",
                )
            return DryRunCallbackReceipt(
                callback_id=callback.callback_id,
                status=DryRunCallbackStatus.DUPLICATE,
                accepted=True,
                duplicate=True,
                would_send=False,
            )
        self._callbacks[callback.callback_id] = copy.deepcopy(payload)
        return DryRunCallbackReceipt(
            callback_id=callback.callback_id,
            status=DryRunCallbackStatus.ACCEPTED,
            accepted=True,
            duplicate=False,
            would_send=True,
        )


def build_final_callback(
    formal: FinalizeReviewStageResult,
    *,
    framework_task_id: str,
    framework_run_id: str,
    attempt_no: int = 1,
) -> StageResultCallback:
    return StageResultCallback(
        schema_version="1.0",
        review_id=formal.review_id,
        attempt_no=attempt_no,
        framework_task_id=framework_task_id,
        framework_run_id=framework_run_id,
        stage_id="finalize_review",
        event_sequence=100,
        callback_id=(
            f"contract:{framework_run_id}:STAGE_RESULT:finalize_review:100"
        ),
        callback_type="STAGE_RESULT",
        result=formal,
        error=None,
    )


def build_success_state_trace(
    *,
    merge_status: Literal["COMPLETED", "SKIPPED"],
) -> list[DirectE2EStateEvent]:
    if _FORMAL_STAGE_MAPPING["verify_evidence"] != "EVIDENCE_VERIFICATION":
        raise DirectE2EError("RISK_E2E_STATE_MACHINE_CHANGED", "Evidence stage mapping changed")
    if _FORMAL_STAGE_MAPPING["finalize_review"] != "FINALIZING":
        raise DirectE2EError("RISK_E2E_STATE_MACHINE_CHANGED", "Final stage mapping changed")
    events = (
        ("RISK_REVIEW_STARTED", "RISK_REVIEW"),
        ("EXTENDED_BUNDLE_COMPLETED", "RISK_REVIEW"),
        ("COMPATIBILITY_COMPLETED", "RISK_REVIEW"),
        (
            "SEMANTIC_MERGE_COMPLETED"
            if merge_status == "COMPLETED"
            else "SEMANTIC_MERGE_SKIPPED",
            "RISK_REVIEW",
        ),
        ("EVIDENCE_VERIFIED", "EVIDENCE_VERIFICATION"),
        ("FORMAL_PAYLOAD_BUILT", "FINALIZING"),
        ("DRY_RUN_RESULT_SINK_ACCEPTED", "FINALIZING"),
        ("DRY_RUN_CALLBACK_ACCEPTED", "FINALIZING"),
        ("DIRECT_E2E_SUCCEEDED", "FINALIZING"),
    )
    return [
        DirectE2EStateEvent(
            sequence=index,
            event=event,
            review_status="SUCCEEDED" if index == len(events) else "RUNNING",
            formal_stage=stage,
        )
        for index, (event, stage) in enumerate(events, start=1)
    ]


class DirectRiskReviewEndToEndRunner:
    """Finalize one already-executed Direct chain with production DTOs in DRY_RUN."""

    def __init__(self, sink: DryRunResultSink | None = None) -> None:
        self.sink = sink or DryRunResultSink()

    def finalize(
        self,
        request: DirectRiskReviewEndToEndRequest,
        *,
        run_id: str,
        compatible: LegacyCompatibleRiskReviewResult,
        compatibility_context: LegacyCompatibilityContext,
        stage_metrics: list[DirectE2EStageMetric],
        framework_task_id: str,
        framework_run_id: str,
        core_signature: str | None = None,
        legal_evidence_bundle: Any | None = None,
    ) -> tuple[DirectRiskReviewEndToEndResult, ReviewResultData]:
        started = time.perf_counter()
        if request.review_id != compatibility_context.review_id:
            raise DirectE2EError("RISK_E2E_INPUT_MISMATCH", "review_id changed")
        formal, payload, payload_hash = build_formal_result(
            compatible,
            context=compatibility_context,
            generation_id=request.generation_id,
            framework_task_id=framework_task_id,
            framework_run_id=framework_run_id,
            legal_evidence_bundle=legal_evidence_bundle,
        )
        receipt = self.sink.submit(payload.model_dump(mode="json"))
        callback = build_final_callback(
            formal,
            framework_task_id=framework_task_id,
            framework_run_id=framework_run_id,
        )
        callback_receipt = self.sink.callback(callback)
        finalization_ms = round((time.perf_counter() - started) * 1000)
        metrics = list(stage_metrics) + [
            DirectE2EStageMetric(
                stage="formal_finalization",
                wall_ms=finalization_ms,
                model_calls=0,
                repair_calls=0,
                tool_calls=0,
                prompt_tokens=0,
                cached_tokens=0,
                completion_tokens=0,
            )
        ]
        result = DirectRiskReviewEndToEndResult(
            run_id=run_id,
            review_id=request.review_id,
            generation_id=request.generation_id,
            contract_hash=request.contract_hash,
            fixture_id=request.fixture_id,
            status="SUCCEEDED",
            extended_bundle_status="SUCCEEDED",
            compatibility_status="SUCCEEDED",
            semantic_merge_status=compatible.merge_status,
            evidence_verification_status=compatible.evidence_verification_status,
            payload_validation_status="VALID",
            dry_run_sink_status=receipt.status,
            dry_run_callback_status=callback_receipt.status,
            formal_result_hash=payload.result_hash,
            formal_payload_hash=payload_hash,
            core_result_signature=core_signature or core_result_signature(compatible),
            stage_metrics=metrics,
            state_transition_trace=build_success_state_trace(
                merge_status=compatible.merge_status
            ),
            sink_receipt=receipt,
            callback_receipt=callback_receipt,
        )
        return result, payload


def finding_level_counts(payload: ReviewResultData) -> dict[str, int]:
    counts = Counter(item.risk_level.value for item in payload.findings)
    return {key: counts[key] for key in ("HIGH", "MEDIUM", "LOW", "INFO")}
