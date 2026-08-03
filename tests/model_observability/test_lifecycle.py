from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from decimal import Decimal

import pytest
from sqlalchemy import text

from model_observability.domain import (
    CostSource,
    DispatchStatus,
    InvocationMetrics,
    InvocationOutcome,
    InvalidInvocationTransitionError,
    ModelInvocationContext,
    ModelInvocationEventType,
    PrivacyMode,
    RouteType,
    model_metadata_registry_document,
    sanitize_metadata,
    utc_now,
)
from model_observability.lifecycle import ModelInvocationLifecycleService
from model_observability.runtime import (
    DatabaseModelInvocationRuntimeRecorder,
    RuntimeInvocationFinalizer,
    RuntimeModelDescriptor,
    RuntimeObservabilityContext,
)


def _context(
    suffix: str,
    *,
    logical_call_id: str | None = None,
    attempt_no: int = 1,
) -> ModelInvocationContext:
    return ModelInvocationContext(
        tenant_id="tenant-a",
        feature_code="contract.review",
        provider="deepseek",
        model_name="deepseek-v4-pro",
        privacy_mode=PrivacyMode.PRIVATE,
        route_type=RouteType.EXTERNAL,
        logical_call_id=logical_call_id or f"logical-{suffix}",
        invocation_id=f"inv-{suffix}",
        attempt_no=attempt_no,
        task_id="task-1",
        run_id="run-1",
    )


@pytest.mark.asyncio
async def test_append_only_lifecycle_is_idempotent_and_hashes_provider_id(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    projection = await service.start_invocation(_context("success"))
    assert projection.lifecycle_status == "RUNNING"
    assert projection.dispatch_status == "NOT_DISPATCHED"
    assert projection.outcome is None

    await service.mark_dispatched(
        projection.invocation_id,
        provider_request_id="provider-secret-looking-id",
    )
    metrics = InvocationMetrics(
        input_tokens=100,
        output_tokens=20,
        latency_ms=321,
        time_to_first_token_ms=42,
        cost_amount=Decimal("0.125"),
        cost_currency="CNY",
        cost_source=CostSource.PROVIDER,
        pricing_version="pricing-1",
        cost_calculated_at=utc_now(),
    )
    terminal = await service.terminate(
        projection.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
        metrics=metrics,
    )
    repeated = await service.terminate(
        projection.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
        metrics=metrics,
    )

    assert terminal.invocation_id == repeated.invocation_id
    assert terminal.lifecycle_status == "TERMINAL"
    assert terminal.outcome == "SUCCESS"
    assert terminal.provider_request_id_hash
    assert terminal.provider_request_id_hash != "provider-secret-looking-id"
    events = await service.get_events(projection.invocation_id)
    assert [event.event_type for event in events] == [
        "MODEL_INVOCATION_STARTED",
        "MODEL_INVOCATION_DISPATCHED",
        "MODEL_INVOCATION_SUCCEEDED",
    ]
    assert all("provider-secret-looking-id" not in str(event.metadata_json) for event in events)

@pytest.mark.asyncio
async def test_runtime_finalizer_persists_provider_finish_reason(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    recorder = DatabaseModelInvocationRuntimeRecorder(service)
    handle = await recorder.begin(
        RuntimeModelDescriptor(
            tenant_id="tenant-a",
            provider="deepseek",
            model_name="deepseek-v4-pro",
            route_type=RouteType.EXTERNAL,
            model_pack_id=None,
        ),
        RuntimeObservabilityContext(
            feature_code="contract.revision",
            logical_call_id="logical-provider-finish",
        ),
    )
    finalizer = RuntimeInvocationFinalizer(
        recorder=recorder,
        handle=handle,
        metrics=InvocationMetrics(input_tokens=10, output_tokens=3, latency_ms=20),
        provider_request_id="provider-request-signed",
        finish_reason="stop",
    )

    await finalizer.succeed()

    events = await service.get_events(handle.invocation_id)
    assert events[-1].event_type == ModelInvocationEventType.SUCCEEDED
    assert events[-1].metadata_json == {"finish_reason": "stop"}
    assert events[-1].provider_request_id_hash
    assert "provider-request-signed" not in repr(events[-1])


@pytest.mark.asyncio
async def test_output_guardrail_is_a_dispatched_denied_terminal(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    invocation = await service.start_invocation(_context("guardrail"))
    with pytest.raises(InvalidInvocationTransitionError):
        await service.terminate(
            invocation.invocation_id,
            event_type=ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED,
            outcome=InvocationOutcome.DENIED,
        )

    await service.mark_dispatched(invocation.invocation_id)
    terminal = await service.terminate(
        invocation.invocation_id,
        event_type=ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED,
        outcome=InvocationOutcome.DENIED,
        metrics=InvocationMetrics(input_tokens=10, output_tokens=2, latency_ms=50),
        metadata={"guardrail_code": "OUTPUT_POLICY_REJECTED"},
    )
    assert terminal.dispatch_status == "DISPATCHED"
    assert terminal.outcome == "DENIED"


@pytest.mark.asyncio
async def test_orphan_reconciliation_never_claims_crash_was_not_dispatched(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    old = utc_now() - timedelta(hours=2)
    not_sent = await service.start_invocation(
        _context("not-sent"), occurred_at=old
    )
    uncertain = await service.start_invocation(
        _context("uncertain"), occurred_at=old
    )
    await service.mark_dispatch_unknown(uncertain.invocation_id, occurred_at=old)

    first = await service.reconcile_orphans(
        older_than=timedelta(minutes=30),
        now=utc_now(),
    )
    second = await service.reconcile_orphans(
        older_than=timedelta(minutes=30),
        now=utc_now(),
    )

    assert first == 2
    assert second == 0
    not_sent_projection = await service.get_projection(not_sent.invocation_id)
    uncertain_projection = await service.get_projection(uncertain.invocation_id)
    assert not_sent_projection.dispatch_status == "DISPATCH_UNKNOWN"
    assert not_sent_projection.outcome == "UNKNOWN"
    assert uncertain_projection.dispatch_status == "DISPATCH_UNKNOWN"
    assert uncertain_projection.outcome == "UNKNOWN"
    assert [
        event.event_type for event in await service.get_events(not_sent.invocation_id)
    ] == [
        "MODEL_INVOCATION_STARTED",
        "MODEL_INVOCATION_OUTCOME_UNKNOWN",
    ]
    assert len(await service.get_events(uncertain.invocation_id)) == 3


@pytest.mark.asyncio
async def test_projection_can_be_completely_rebuilt_from_events(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    first = await service.start_invocation(_context("rebuild-1"))
    await service.mark_dispatched(first.invocation_id)
    await service.terminate(
        first.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
        metrics=InvocationMetrics(latency_ms=10),
    )
    second = await service.start_invocation(_context("rebuild-2"))
    await service.terminate(
        second.invocation_id,
        event_type=ModelInvocationEventType.FAILED,
        outcome=InvocationOutcome.FAILURE,
        error_code="MODEL_REQUEST_NOT_DISPATCHED",
    )
    before = {
        first.invocation_id: (
            (await service.get_projection(first.invocation_id)).projection_version,
            "SUCCESS",
        ),
        second.invocation_id: (
            (await service.get_projection(second.invocation_id)).projection_version,
            "FAILURE",
        ),
    }

    rebuilt = await service.rebuild_projection()

    assert rebuilt == 2
    for invocation_id, (version, outcome) in before.items():
        projection = await service.get_projection(invocation_id)
        assert projection.projection_version == version
        assert projection.outcome == outcome


@pytest.mark.asyncio
async def test_rebuild_orders_equal_business_timestamps_by_ingestion_and_lifecycle(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    occurred_at = utc_now() - timedelta(minutes=5)
    invocation = await service.start_invocation(
        _context("same-time"),
        occurred_at=occurred_at,
    )
    await service.mark_dispatched(
        invocation.invocation_id,
        occurred_at=occurred_at,
    )
    await service.terminate(
        invocation.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
        occurred_at=occurred_at,
    )

    assert await service.rebuild_projection() == 1
    projection = await service.get_projection(invocation.invocation_id)
    assert projection.projection_version == 3
    assert projection.dispatch_status == "DISPATCHED"
    assert projection.outcome == "SUCCESS"


@pytest.mark.asyncio
async def test_unregistered_or_sensitive_metadata_is_rejected_before_write(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    with pytest.raises(ValueError):
        await service.start_invocation(
            _context("secret"),
            metadata={"prompt": "must never be persisted"},
        )


@pytest.mark.asyncio
async def test_logical_call_attempt_cannot_be_reassigned_to_another_invocation(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    await service.start_invocation(
        _context("owner", logical_call_id="logical-shared", attempt_no=2)
    )

    with pytest.raises(InvalidInvocationTransitionError):
        await service.start_invocation(
            _context("intruder", logical_call_id="logical-shared", attempt_no=2)
        )


_CONTEXT_LIMITS = {
    "tenant_id": 64,
    "feature_code": 120,
    "provider": 80,
    "model_name": 160,
    "logical_call_id": 80,
    "invocation_id": 80,
    "fallback_from_invocation_id": 80,
    "user_id": 120,
    "task_id": 80,
    "run_id": 80,
    "stage_id": 120,
    "request_id": 120,
    "trace_id": 80,
    "model_pack_id": 120,
    "model_pack_version": 64,
    "deployment_name": 160,
    "provider_region": 80,
    "model_config_id": 120,
    "service_version": 80,
}


@pytest.mark.parametrize(("field_name", "limit"), _CONTEXT_LIMITS.items())
def test_context_fixed_columns_enforce_type_length_and_safe_format(
    field_name,
    limit,
):
    context = _context("fixed-fields")
    with pytest.raises(ValueError):
        replace(context, **{field_name: 123}).validate()
    with pytest.raises(ValueError):
        replace(context, **{field_name: "x" * (limit + 1)}).validate()
    with pytest.raises(ValueError):
        replace(context, **{field_name: "unsafe value"}).validate()


def test_context_fixed_columns_accept_registered_identifier_characters():
    ModelInvocationContext(
        tenant_id="tenant/._:+-01",
        feature_code="contract.review/v2:+-",
        provider="provider/._:+-01",
        model_name="org/model.v2:prod+safe",
        privacy_mode=PrivacyMode.STANDARD,
        route_type=RouteType.EXTERNAL,
        logical_call_id="logical/._:+-01",
        invocation_id="inv/._:+-01",
        fallback_from_invocation_id="inv/._:+-00",
        user_id="user/._:+-01",
        task_id="task/._:+-01",
        run_id="run/._:+-01",
        stage_id="stage/._:+-01",
        request_id="request/._:+-01",
        trace_id="trace/._:+-01",
        model_pack_id="pack/._:+-01",
        model_pack_version="version/._:+-01",
        deployment_name="deployment/._:+-01",
        provider_region="region/._:+-01",
        model_config_id="config/._:+-01",
        service_version="service/._:+-01",
    ).validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("trace_id", "eyJabcdefghijk.abcdefghijk.abcdefghijk"),
        ("request_id", "request\nsecret"),
        ("user_id", "这是合同正文而不是标识符"),
    ],
)
def test_context_fixed_columns_reject_sensitive_or_free_text(field_name, value):
    with pytest.raises(ValueError):
        replace(_context("sensitive"), **{field_name: value}).validate()


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param(
            "eyJabcdefghijk.abcdefghijk.abcdefghijk",
            id="jwt",
        ),
        pytest.param("hwm_A_-z09Bc-d", id="page3-high-watermark"),
        pytest.param("cur_ABCDEFGHIJKLMNOP", id="cursor"),
        pytest.param(
            "crf_revfin_v1.P." + "F" * 43,
            id="page2-revision-finalize",
        ),
        pytest.param(
            "qtk_qs_" + "Q" * 43 + "." + "S" * 43,
            id="page4-query-token",
        ),
        pytest.param(
            "cap.P." + "G" * 43,
            id="page3-cursor-retry",
        ),
        pytest.param(
            "obs1.test.MODEL." + "R" * 20 + ".1." + "S" * 43,
            id="internal-capability",
        ),
        pytest.param(
            "sk-proj-" + "K" * 32,
            id="api-key",
        ),
        pytest.param(
            "Bearer-leaked-credential",
            id="bearer-credential",
        ),
        pytest.param(
            "Authorization-leaked-credential",
            id="authorization-credential",
        ),
    ],
)
@pytest.mark.parametrize("embedded", [False, True], ids=["exact", "embedded"])
def test_context_fixed_columns_reject_tokens_and_capabilities(secret, embedded):
    value = f"head-{secret}-tail" if embedded else secret
    assert len(value) <= _CONTEXT_LIMITS["request_id"]

    with pytest.raises(ValueError):
        replace(_context("capability"), request_id=value).validate()


@pytest.mark.parametrize(
    "pricing_version",
    [
        "head-eyJabcdefghijk.abcdefghijk.abcdefghijk-tail",
        "head-hwm_A_-z09Bc-d-tail",
    ],
)
def test_pricing_version_rejects_embedded_tokens_and_capabilities(pricing_version):
    with pytest.raises(ValueError):
        InvocationMetrics(
            cost_amount=Decimal("1"),
            cost_currency="USD",
            cost_source=CostSource.PROVIDER,
            pricing_version=pricing_version,
        ).validate()


@pytest.mark.parametrize(
    "value",
    [
        "eyJshort.segment.signature",
        "hwm_status",
        "cur_small",
        "crf_revfin_v1.short.short",
        "qtk_qs_short",
        "cap.short.short",
        "obs1.test.MODEL.short.1.short",
    ],
)
def test_context_fixed_columns_allow_incomplete_capability_like_identifiers(value):
    replace(_context("ordinary"), request_id=value).validate()


def test_context_requires_real_enums_and_integer_attempt():
    with pytest.raises(ValueError):
        replace(_context("enum"), privacy_mode="PRIVATE").validate()
    with pytest.raises(ValueError):
        replace(_context("enum"), route_type="EXTERNAL").validate()
    with pytest.raises(ValueError):
        replace(_context("enum"), attempt_no=True).validate()


@pytest.mark.asyncio
async def test_provider_hash_cannot_change_or_disappear_and_null_can_enrich(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    changed = await service.start_invocation(_context("hash-change"))
    await service.mark_dispatched(
        changed.invocation_id,
        provider_request_id="provider-request-a",
    )
    same_dispatch = await service.mark_dispatched(
        changed.invocation_id,
        provider_request_id="provider-request-a",
    )
    assert same_dispatch.provider_request_id_hash
    with pytest.raises(InvalidInvocationTransitionError):
        await service.mark_dispatched(changed.invocation_id)
    with pytest.raises(InvalidInvocationTransitionError):
        await service.mark_dispatched(
            changed.invocation_id,
            provider_request_id="provider-request-b",
        )
    with pytest.raises(InvalidInvocationTransitionError):
        await service.terminate(
            changed.invocation_id,
            event_type=ModelInvocationEventType.SUCCEEDED,
            outcome=InvocationOutcome.SUCCESS,
            provider_request_id="provider-request-b",
        )
    terminal = await service.terminate(
        changed.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
    )
    original_hash = terminal.provider_request_id_hash
    assert original_hash

    async with isolated_model_store() as session:
        await session.execute(
            text(
                "UPDATE tuge_model_invocation_event "
                "SET provider_request_id_hash = NULL "
                "WHERE invocation_id = :invocation_id "
                "AND event_type = 'MODEL_INVOCATION_SUCCEEDED'"
            ),
            {"invocation_id": changed.invocation_id},
        )
    with pytest.raises(InvalidInvocationTransitionError):
        await service.rebuild_projection(batch_size=1)
    assert (
        await service.get_projection(changed.invocation_id)
    ).provider_request_id_hash == original_hash

    enriched = await service.start_invocation(_context("hash-enrich"))
    await service.mark_dispatched(enriched.invocation_id)
    await service.mark_dispatched(enriched.invocation_id)
    with pytest.raises(InvalidInvocationTransitionError):
        await service.mark_dispatched(
            enriched.invocation_id,
            provider_request_id="provider-request-too-late-for-dispatch",
        )
    enriched_terminal = await service.terminate(
        enriched.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
        provider_request_id="provider-request-late",
    )
    assert enriched_terminal.provider_request_id_hash
    idempotent_terminal = await service.terminate(
        enriched.invocation_id,
        event_type=ModelInvocationEventType.SUCCEEDED,
        outcome=InvocationOutcome.SUCCESS,
    )
    assert (
        idempotent_terminal.provider_request_id_hash
        == enriched_terminal.provider_request_id_hash
    )


@pytest.mark.asyncio
async def test_lifecycle_rejects_naive_or_backwards_business_time(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    with pytest.raises(ValueError):
        await service.start_invocation(
            _context("naive-time"),
            occurred_at=utc_now().replace(tzinfo=None),
        )

    started_at = utc_now()
    invocation = await service.start_invocation(
        _context("ordered-time"),
        occurred_at=started_at,
    )
    with pytest.raises(InvalidInvocationTransitionError):
        await service.mark_dispatched(
            invocation.invocation_id,
            occurred_at=started_at - timedelta(seconds=1),
        )
    dispatched_at = started_at + timedelta(seconds=1)
    await service.mark_dispatched(
        invocation.invocation_id,
        occurred_at=dispatched_at,
    )
    with pytest.raises(InvalidInvocationTransitionError):
        await service.terminate(
            invocation.invocation_id,
            event_type=ModelInvocationEventType.SUCCEEDED,
            outcome=InvocationOutcome.SUCCESS,
            occurred_at=started_at,
        )


@pytest.mark.asyncio
async def test_uncertain_dispatch_cannot_claim_usage_or_cost(
    isolated_model_store,
) -> None:
    service = ModelInvocationLifecycleService(isolated_model_store)
    invocation = await service.start_invocation(_context("uncertain-usage"))
    await service.mark_dispatch_unknown(invocation.invocation_id)
    with pytest.raises(InvalidInvocationTransitionError):
        await service.terminate(
            invocation.invocation_id,
            event_type=ModelInvocationEventType.FAILED,
            outcome=InvocationOutcome.FAILURE,
            metrics=InvocationMetrics(input_tokens=1, latency_ms=10),
            error_code="MODEL_PROVIDER_DISPATCH_ERROR",
        )


def test_model_metadata_registry_is_event_scoped_and_matches_handoff() -> None:
    registry_path = (
        Path(__file__).parents[2]
        / "backend"
        / "model_observability"
        / "model_metadata_registry.v1.json"
    )
    handoff = json.loads(registry_path.read_text("utf-8"))
    assert handoff == model_metadata_registry_document()
    assert handoff["keyStyle"] == "snake_case"
    assert handoff["topLevelFieldsNotMetadata"] == [
        "attemptNo",
        "fallbackFromInvocationId",
        "featureCode",
        "timeToFirstTokenMs",
    ]

    dto_samples = {
        ModelInvocationEventType.STARTED: {"worker_lease_state": "ACQUIRED"},
        ModelInvocationEventType.DISPATCHED: {},
        ModelInvocationEventType.DISPATCH_UNKNOWN: {
            "dispatch_evidence": "missing_after_orphan_timeout",
            "reconciliation_reason": "orphan_timeout",
        },
        ModelInvocationEventType.SUCCEEDED: {
            "finish_reason": "stop"
        },
        ModelInvocationEventType.FAILED: {},
        ModelInvocationEventType.VALIDATION_FAILED: {
            "schema_validation_code": "MODEL_OUTPUT_SCHEMA_INVALID"
        },
        ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED: {
            "guardrail_code": "OUTPUT_POLICY_REJECTED"
        },
        ModelInvocationEventType.OUTCOME_UNKNOWN: {
            "reconciliation_reason": "orphan_timeout"
        },
        ModelInvocationEventType.ABANDONED: {
            "worker_lease_state": "EXPIRED"
        },
    }
    for event_type, metadata in dto_samples.items():
        assert sanitize_metadata(
            metadata,
            event_type=event_type,
            schema_version=1,
        ) == metadata

    with pytest.raises(ValueError):
        sanitize_metadata(
            {"guardrail_code": "OUTPUT_POLICY_REJECTED"},
            event_type=ModelInvocationEventType.SUCCEEDED,
        )
    with pytest.raises(ValueError):
        sanitize_metadata(
            {"finishReason": "provider_completed"},
            event_type=ModelInvocationEventType.SUCCEEDED,
        )
    with pytest.raises(ValueError):
        sanitize_metadata(
            {"attemptNo": "2"},
            event_type=ModelInvocationEventType.STARTED,
        )
