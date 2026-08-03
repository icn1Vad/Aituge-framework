from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class DtoTransform:
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    rule: str
    risk: str


@dataclass(frozen=True)
class DtoMapping:
    internal_schema: str
    external_schema: str
    direct: tuple[str, ...]
    renamed: dict[str, str]
    transformed: tuple[DtoTransform, ...]
    synthesized: dict[str, str]
    defaulted: dict[str, str]
    consumed: dict[str, str]
    defaulted_null: dict[str, str] = field(default_factory=dict)


def deterministic_trace_id(trace_ids: Iterable[str]) -> str | None:
    """Select one stable external trace ID without trusting source array order."""

    candidates = sorted({trace_id for trace_id in trace_ids if trace_id})
    return candidates[0] if candidates else None


DTO_MAPPING_MATRIX = {
    "TASK_SUMMARY": DtoMapping(
        internal_schema="InternalTask",
        external_schema="TaskSummary",
        direct=(
            "taskId",
            "projectionVersion",
            "dataAsOf",
            "featureCode",
            "tenantId",
            "initiator",
            "status",
            "stage",
            "progress",
            "createdAt",
            "startedAt",
            "finishedAt",
            "durationMs",
            "retryCount",
            "privacyMode",
            "routeType",
            "errorCode",
        ),
        renamed={"subject.type": "subjectType", "subject.id": "subjectId"},
        transformed=(
            DtoTransform(
                sources=("currentRun",),
                targets=("runId", "currentRunStatus"),
                rule=(
                    "copy currentRun.runId/status; emit null when absent; "
                    "attempt and timestamps remain internal detail"
                ),
                risk="future run-detail DTOs must not infer discarded attempt timing",
            ),
            DtoTransform(
                sources=("requestId", "traceIds"),
                targets=("correlation",),
                rule=(
                    "copy requestId; use mapped taskId/currentRun.runId; set traceId "
                    "to deterministic_trace_id(traceIds); leave unrelated members null"
                ),
                risk=(
                    "traceIds ordering is not frozen; sort unique non-empty values and "
                    "select the lexicographically first value"
                ),
            ),
        ),
        synthesized={"environment": "deployment-bound environment"},
        defaulted={},
        consumed={
            "ingestedAt": "source freshness and dataAsOf validation",
            "schemaVersion": "internal contract version validation",
        },
    ),
    "TASK_EVENT": DtoMapping(
        internal_schema="InternalTaskEvent",
        external_schema="UnifiedEvent",
        direct=(
            "eventId",
            "sequence",
            "schemaVersion",
            "tenantId",
            "occurredAt",
            "ingestedAt",
            "displayCode",
            "outcome",
            "errorCode",
            "durationMs",
            "metadata",
        ),
        renamed={"eventType": "eventName"},
        transformed=(
            DtoTransform(
                sources=("taskId", "runId", "stageId", "requestId", "traceId"),
                targets=("correlation",),
                rule=(
                    "construct task/run/request/trace Correlation; retain stageId only "
                    "in registered metadata because Correlation has no stageId"
                ),
                risk="missing members remain explicit null and are never guessed",
            ),
        ),
        synthesized={
            "source": "TASK_EVENT constant",
            "service": "authenticated producer identity",
            "environment": "deployment-bound environment",
            "hasDetail": "detail capability registry",
            "sensitivity": "event registry",
        },
        defaulted={"level": "event registry default"},
        consumed={
            "agentName": "registered metadata",
            "toolName": "registered metadata",
        },
        defaulted_null={
            "aggregateVersion": "not an aggregate-version event",
            "serviceVersion": "no registered producer version",
            "displayArgs": "registry supplies no arguments",
            "displayMessage": "client resolves displayCode",
            "scopeType": "not a security event",
            "actor": "not supplied",
            "subjectType": "not supplied",
            "subjectId": "not supplied",
            "security": "not a security event",
            "model": "not a model event",
        },
    ),
    "MODEL_INVOCATION_SUMMARY": DtoMapping(
        internal_schema="InternalModelInvocation",
        external_schema="ModelInvocationSummary",
        direct=(
            "invocationId",
            "projectionVersion",
            "dataAsOf",
            "logicalCallId",
            "attemptNo",
            "fallbackFromInvocationId",
            "tenantId",
            "featureCode",
            "taskId",
            "runId",
            "stageId",
            "startedAt",
            "finishedAt",
            "lifecycleStatus",
            "outcome",
            "errorCode",
            "latencyMs",
            "timeToFirstTokenMs",
        ),
        renamed={
            "provider": "model.provider",
            "modelPackId": "model.modelPackId",
            "modelPackVersion": "model.modelPackVersion",
            "modelName": "model.modelName",
            "deploymentName": "model.deploymentName",
            "providerRegion": "model.providerRegion",
            "privacyMode": "model.privacyMode",
            "routeType": "model.routeType",
            "dispatchStatus": "model.dispatchStatus",
            "inputTokens": "model.inputTokens",
            "outputTokens": "model.outputTokens",
            "costAmount": "model.costAmount",
            "costCurrency": "model.costCurrency",
            "costSource": "model.costSource",
            "pricingVersion": "model.pricingVersion",
            "costCalculatedAt": "model.costCalculatedAt",
        },
        transformed=(),
        synthesized={"dispatchStatus": "copy validated model.dispatchStatus"},
        defaulted={},
        consumed={
            "requestId": "detail-only correlation",
            "traceId": "detail-only correlation",
            "ingestedAt": "source freshness and dataAsOf validation",
            "providerRequestIdHash": "restricted detail-only identifier",
        },
    ),
    "MODEL_EVENT": DtoMapping(
        internal_schema="InternalModelInvocationEvent",
        external_schema="UnifiedEvent",
        direct=(
            "eventId",
            "schemaVersion",
            "tenantId",
            "occurredAt",
            "ingestedAt",
            "outcome",
            "errorCode",
            "metadata",
        ),
        renamed={
            "eventType": "eventName",
            "provider": "model.provider",
            "modelPackId": "model.modelPackId",
            "modelPackVersion": "model.modelPackVersion",
            "modelName": "model.modelName",
            "deploymentName": "model.deploymentName",
            "providerRegion": "model.providerRegion",
            "privacyMode": "model.privacyMode",
            "routeType": "model.routeType",
            "dispatchStatus": "model.dispatchStatus",
            "inputTokens": "model.inputTokens",
            "outputTokens": "model.outputTokens",
            "costAmount": "model.costAmount",
            "costCurrency": "model.costCurrency",
            "costSource": "model.costSource",
            "pricingVersion": "model.pricingVersion",
            "costCalculatedAt": "model.costCalculatedAt",
        },
        transformed=(
            DtoTransform(
                sources=(
                    "logicalCallId",
                    "invocationId",
                    "taskId",
                    "runId",
                    "stageId",
                    "requestId",
                    "traceId",
                ),
                targets=("correlation",),
                rule=(
                    "construct logicalCall/invocation/task/run/request/trace Correlation; "
                    "retain stageId only in registered metadata"
                ),
                risk="missing members remain explicit null and are never guessed",
            ),
            DtoTransform(
                sources=("latencyMs",),
                targets=("durationMs",),
                rule=(
                    "copy actual end-to-end latencyMs only for terminal invocation "
                    "events; emit null for nonterminal events"
                ),
                risk=(
                    "never use timeToFirstTokenMs and never claim terminal latency "
                    "before a terminal event"
                ),
            ),
        ),
        synthesized={
            "source": "MODEL_EVENT constant",
            "service": "authenticated producer identity",
            "environment": "deployment-bound environment",
            "level": "event registry",
            "displayCode": "event registry",
            "hasDetail": "detail capability registry",
            "sensitivity": "event registry",
        },
        defaulted={},
        consumed={
            "attemptNo": "registered metadata",
            "fallbackFromInvocationId": "registered metadata",
            "featureCode": "registered metadata",
            "providerRequestIdHash": "restricted detail-only identifier",
            "timeToFirstTokenMs": "registered model metadata; never event duration",
        },
        defaulted_null={
            "sequence": "no frozen model-event sequence",
            "aggregateVersion": "not a business aggregate version",
            "serviceVersion": "no registered producer version",
            "displayArgs": "registry supplies no arguments",
            "displayMessage": "client resolves displayCode",
            "scopeType": "not a security event",
            "actor": "not supplied",
            "subjectType": "not supplied",
            "subjectId": "not supplied",
            "security": "not a security event",
        },
    ),
    "MODEL_SUMMARY": DtoMapping(
        internal_schema="InternalModelSummary",
        external_schema="ModelSummaryResponse",
        direct=(),
        renamed={
            "logicalCallCount": "logicalCalls.count",
            "logicalSuccessRate": "logicalCalls.successRate",
            "retryRate": "logicalCalls.retryRate",
            "dispatchAttemptCount": "attempts.dispatchAttemptCount",
            "dispatchedRequestCount": "attempts.dispatchedRequestCount",
            "notDispatchedCount": "attempts.notDispatchedCount",
            "dispatchUnknownCount": "attempts.dispatchUnknownCount",
            "activeAttemptCount": "attempts.activeCount",
            "attemptSuccessRate": "attempts.successRate",
            "inputTokens": "attempts.inputTokens",
            "outputTokens": "attempts.outputTokens",
            "costs": "attempts.costs",
        },
        transformed=(),
        synthesized={"sourceStatus": "Java source coordinator"},
        defaulted={
            "latencyPercentiles": "zero percentiles when omitted",
            "ttftPercentiles": "zero percentiles when omitted",
            "timeSeries": "empty array when omitted",
            "errorBreakdown": "empty array when omitted",
            "providerBreakdown": "empty array when omitted",
            "modelBreakdown": "empty array when omitted",
            "routeTypeBreakdown": "empty array when omitted",
            "privacyModeBreakdown": "empty array when omitted",
        },
        consumed={},
    ),
    "SECURITY_EVENT": DtoMapping(
        internal_schema="InternalSecurityEvent",
        external_schema="SecurityEvent",
        direct=(
            "eventId",
            "auditActionId",
            "accessSessionId",
            "auditLayer",
            "parentAuditEventId",
            "scopeType",
            "schemaVersion",
            "service",
            "tenantId",
            "occurredAt",
            "ingestedAt",
            "category",
            "action",
            "riskLevel",
            "actor",
            "missingContextReason",
            "crossTenant",
            "reasonCode",
            "sourceIpMasked",
            "outcome",
            "errorCode",
            "displayCode",
            "metadata",
        ),
        renamed={"subject.type": "subjectType", "subject.id": "subjectId"},
        transformed=(
            DtoTransform(
                sources=("requestId", "traceId"),
                targets=("correlation",),
                rule="construct requestId/traceId Correlation; leave other members null",
                risk="never enrich from another tenant or request",
            ),
        ),
        synthesized={
            "environment": "deployment-bound environment",
            "displayArgs": "security event registry",
            "displayMessage": "redacted display formatter",
        },
        defaulted={},
        consumed={},
    ),
    "STAGE_SUMMARY": DtoMapping(
        internal_schema="InternalStage",
        external_schema="StageSummary",
        direct=(
            "stageId",
            "name",
            "status",
            "startedAt",
            "finishedAt",
            "durationMs",
            "errorCode",
        ),
        renamed={},
        transformed=(),
        synthesized={},
        defaulted={},
        consumed={
            "runId": "request path binding",
            "sequence": "fixed result ordering",
            "retryCount": "not exposed by frozen external summary",
        },
    ),
}


KNOWN_FROZEN_SCHEMA_GAPS = {
    ("SECURITY_EVENT", "sourceIpMasked", "sourceIpMasked"): (
        "frozen 04 caps sourceIpMasked at 64 although registered "
        "hmac-sha256 plus digest is 76 characters"
    ),
}
