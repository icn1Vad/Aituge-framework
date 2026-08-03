from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


class DispatchStatus(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    DISPATCHED = "DISPATCHED"
    DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"


class InvocationLifecycleStatus(StrEnum):
    RUNNING = "RUNNING"
    TERMINAL = "TERMINAL"


class InvocationOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    ABANDONED = "ABANDONED"


class ModelInvocationEventType(StrEnum):
    STARTED = "MODEL_INVOCATION_STARTED"
    DISPATCHED = "MODEL_INVOCATION_DISPATCHED"
    DISPATCH_UNKNOWN = "MODEL_INVOCATION_DISPATCH_UNKNOWN"
    SUCCEEDED = "MODEL_INVOCATION_SUCCEEDED"
    FAILED = "MODEL_INVOCATION_FAILED"
    VALIDATION_FAILED = "MODEL_INVOCATION_VALIDATION_FAILED"
    OUTPUT_GUARDRAIL_REJECTED = "MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED"
    OUTCOME_UNKNOWN = "MODEL_INVOCATION_OUTCOME_UNKNOWN"
    ABANDONED = "MODEL_INVOCATION_ABANDONED"


class PrivacyMode(StrEnum):
    STANDARD = "STANDARD"
    PRIVATE = "PRIVATE"


class RouteType(StrEnum):
    LOCAL = "LOCAL"
    EXTERNAL = "EXTERNAL"


class CostSource(StrEnum):
    PROVIDER = "PROVIDER"
    ESTIMATED = "ESTIMATED"


TERMINAL_EVENT_TYPES = frozenset(
    {
        ModelInvocationEventType.SUCCEEDED,
        ModelInvocationEventType.FAILED,
        ModelInvocationEventType.VALIDATION_FAILED,
        ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED,
        ModelInvocationEventType.OUTCOME_UNKNOWN,
        ModelInvocationEventType.ABANDONED,
    }
)

DISPATCH_EVENT_TYPES = frozenset(
    {
        ModelInvocationEventType.DISPATCHED,
        ModelInvocationEventType.DISPATCH_UNKNOWN,
    }
)

_TERMINAL_OUTCOME_RULES: dict[ModelInvocationEventType, frozenset[InvocationOutcome]] = {
    ModelInvocationEventType.SUCCEEDED: frozenset({InvocationOutcome.SUCCESS}),
    ModelInvocationEventType.FAILED: frozenset(
        {
            InvocationOutcome.FAILURE,
            InvocationOutcome.CANCELLED,
            InvocationOutcome.TIMEOUT,
            InvocationOutcome.PARTIAL,
        }
    ),
    ModelInvocationEventType.VALIDATION_FAILED: frozenset({InvocationOutcome.FAILURE}),
    ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED: frozenset(
        {InvocationOutcome.DENIED}
    ),
    ModelInvocationEventType.OUTCOME_UNKNOWN: frozenset({InvocationOutcome.UNKNOWN}),
    ModelInvocationEventType.ABANDONED: frozenset({InvocationOutcome.ABANDONED}),
}

MODEL_PROVIDER_FINISH_REASONS = frozenset(
    {
        "content_filter",
        "function_call",
        "length",
        "stop",
        "tool_calls",
    }
)

_METADATA_ENUMS: dict[str, frozenset[str]] = {
    "dispatch_evidence": frozenset({"missing_after_orphan_timeout"}),
    # provider_completed remains registered for already-persisted v1 rows.
    "finish_reason": MODEL_PROVIDER_FINISH_REASONS | {"provider_completed"},
    "reconciliation_reason": frozenset({"orphan_timeout"}),
    "worker_lease_state": frozenset(
        {"ACQUIRED", "RENEWED", "RELEASED", "EXPIRED"}
    ),
}
_METADATA_CODE_KEYS = frozenset(
    {"guardrail_code", "schema_validation_code"}
)
_EVENT_METADATA_KEYS: dict[ModelInvocationEventType, frozenset[str]] = {
    ModelInvocationEventType.STARTED: frozenset({"worker_lease_state"}),
    ModelInvocationEventType.DISPATCHED: frozenset(),
    ModelInvocationEventType.DISPATCH_UNKNOWN: frozenset(
        {"dispatch_evidence", "reconciliation_reason"}
    ),
    ModelInvocationEventType.SUCCEEDED: frozenset({"finish_reason"}),
    ModelInvocationEventType.FAILED: frozenset(),
    ModelInvocationEventType.VALIDATION_FAILED: frozenset(
        {"schema_validation_code"}
    ),
    ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED: frozenset(
        {"guardrail_code"}
    ),
    ModelInvocationEventType.OUTCOME_UNKNOWN: frozenset(
        {"reconciliation_reason"}
    ),
    ModelInvocationEventType.ABANDONED: frozenset(
        {"reconciliation_reason", "worker_lease_state"}
    ),
}
_EVENT_METADATA_REQUIRED: dict[ModelInvocationEventType, frozenset[str]] = {
    event_type: frozenset() for event_type in ModelInvocationEventType
}
_FORBIDDEN_METADATA_KEY = re.compile(
    r"(?:authorization|cookie|credential|secret|api[_-]?key|prompt|response|contract|attachment|password)",
    re.IGNORECASE,
)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_STABLE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,119}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._:+-]*$")
_JWT_LIKE = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)
_CROSS_PAGE_CAPABILITY = re.compile(
    r"(?:"
    r"qtk_qs_[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}"
    r"|hwm_[A-Za-z0-9_-]{10,80}|cur_[A-Za-z0-9_-]{16,512}={0,2}"
    r"|cap\.[A-Za-z0-9_-]{1,4048}\.[A-Za-z0-9_-]{43}"
    r"|crf_revfin_v1\.[A-Za-z0-9_-]{1,4038}\.[A-Za-z0-9_-]{43}"
    r"|obs1\.(?:test|formal)\.[A-Z_]+\.[A-Za-z0-9_-]{20,64}\.[0-9]{1,12}\.[A-Za-z0-9_-]{43}"
    r")"
)
_CREDENTIAL_LIKE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|(?:bearer|authorization|cookie|session|api[_-]?key|secret|password|credential)"
    r"[-_:./][A-Za-z0-9._:+/-]{16,}"
    r")",
    re.IGNORECASE,
)
_CONTEXT_FIELD_LIMITS: dict[str, int] = {
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
_REQUIRED_CONTEXT_FIELDS = frozenset(
    {
        "tenant_id",
        "feature_code",
        "provider",
        "model_name",
        "logical_call_id",
        "invocation_id",
    }
)


class ModelInvocationError(RuntimeError):
    code = "MODEL_INVOCATION_ERROR"


class InvocationNotFoundError(ModelInvocationError):
    code = "MODEL_INVOCATION_NOT_FOUND"


class InvalidInvocationTransitionError(ModelInvocationError):
    code = "MODEL_INVOCATION_INVALID_TRANSITION"


class ModelSnapshotError(ModelInvocationError):
    code = "MODEL_OBSERVABILITY_SNAPSHOT_INVALID"


class DuplicateModelInvocationAttemptError(InvalidInvocationTransitionError):
    code = "MODEL_INVOCATION_DUPLICATE_ATTEMPT"


class ModelSnapshotCapacityError(ModelSnapshotError):
    code = "OBSERVABILITY_SNAPSHOT_CAPACITY_EXCEEDED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def contains_sensitive_identifier_value(value: str) -> bool:
    """Detect credentials/capabilities even when embedded in an identifier."""

    return bool(
        _JWT_LIKE.search(value)
        or _CROSS_PAGE_CAPABILITY.search(value)
        or _CREDENTIAL_LIKE.search(value)
    )


def hash_provider_request_id(value: str | None) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("provider_request_id must be a string")
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelInvocationContext:
    tenant_id: str
    feature_code: str
    provider: str
    model_name: str
    privacy_mode: PrivacyMode
    route_type: RouteType
    attempt_no: int = 1
    logical_call_id: str = field(default_factory=lambda: new_id("logical"))
    invocation_id: str = field(default_factory=lambda: new_id("inv"))
    fallback_from_invocation_id: str | None = None
    user_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    model_pack_id: str | None = None
    model_pack_version: str | None = None
    deployment_name: str | None = None
    provider_region: str | None = None
    model_config_id: str | None = None
    service_version: str | None = None

    def validate(self) -> None:
        for name, maximum in _CONTEXT_FIELD_LIMITS.items():
            value = getattr(self, name)
            if value is None:
                if name in _REQUIRED_CONTEXT_FIELDS:
                    raise ValueError(f"{name} is required")
                continue
            if type(value) is not str:
                raise ValueError(f"{name} must be a string")
            if not value:
                raise ValueError(f"{name} cannot be empty")
            if len(value) > maximum:
                raise ValueError(f"{name} exceeds its database column length")
            if _SAFE_IDENTIFIER.fullmatch(value) is None:
                raise ValueError(
                    f"{name} must use the registered identifier format"
                )
            if contains_sensitive_identifier_value(value):
                raise ValueError(f"{name} cannot contain a token or capability value")

        if not isinstance(self.privacy_mode, PrivacyMode):
            raise ValueError("privacy_mode must be a PrivacyMode enum")
        if not isinstance(self.route_type, RouteType):
            raise ValueError("route_type must be a RouteType enum")
        if isinstance(self.attempt_no, bool) or not isinstance(self.attempt_no, int):
            raise ValueError("attempt_no must be an integer")
        if self.attempt_no < 1:
            raise ValueError("attempt_no must be >= 1")
        if self.attempt_no > 2_147_483_647:
            raise ValueError("attempt_no is outside the PostgreSQL integer range")
        if self.fallback_from_invocation_id == self.invocation_id:
            raise ValueError(
                "fallback_from_invocation_id cannot reference invocation_id"
            )

@dataclass(frozen=True, slots=True)
class InvocationMetrics:
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    time_to_first_token_ms: int | None = None
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    cost_source: CostSource | None = None
    pricing_version: str | None = None
    cost_calculated_at: datetime | None = None

    def validate(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "time_to_first_token_ms",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 0 or value > 9_223_372_036_854_775_807:
                raise ValueError(
                    f"{name} is outside the PostgreSQL bigint range"
                )
        if (
            self.latency_ms is not None
            and self.time_to_first_token_ms is not None
            and self.time_to_first_token_ms > self.latency_ms
        ):
            raise ValueError("time_to_first_token_ms cannot exceed latency_ms")

        if self.cost_amount is None:
            if any(
                value is not None
                for value in (
                    self.cost_currency,
                    self.cost_source,
                    self.pricing_version,
                    self.cost_calculated_at,
                )
            ):
                raise ValueError("cost metadata requires cost_amount")
            return
        if not isinstance(self.cost_amount, Decimal):
            raise ValueError("cost_amount must be a Decimal")
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("cost_amount must be finite and non-negative")
        normalized_cost = self.cost_amount.normalize()
        fractional_digits = max(0, -normalized_cost.as_tuple().exponent)
        if (
            fractional_digits > 8
            or self.cost_amount >= Decimal("1000000000000")
        ):
            raise ValueError("cost_amount exceeds numeric(20, 8) precision")
        if (
            type(self.cost_currency) is not str
            or _CURRENCY.fullmatch(self.cost_currency) is None
        ):
            raise ValueError(
                "cost_currency must be an ISO 4217 three-letter code"
            )
        if not isinstance(self.cost_source, CostSource):
            raise ValueError(
                "cost_source must be a CostSource enum when cost is present"
            )
        if self.pricing_version is not None:
            if (
                type(self.pricing_version) is not str
                or not self.pricing_version
                or len(self.pricing_version) > 80
                or _SAFE_IDENTIFIER.fullmatch(self.pricing_version) is None
                or contains_sensitive_identifier_value(self.pricing_version)
            ):
                raise ValueError(
                    "pricing_version must use the registered identifier format"
                )
        if self.cost_calculated_at is not None:
            if (
                not isinstance(self.cost_calculated_at, datetime)
                or self.cost_calculated_at.tzinfo is None
                or self.cost_calculated_at.utcoffset() is None
            ):
                raise ValueError(
                    "cost_calculated_at must be timezone-aware"
                )


def validate_terminal_outcome(
    event_type: ModelInvocationEventType,
    outcome: InvocationOutcome,
    dispatch_status: DispatchStatus,
) -> None:
    allowed = _TERMINAL_OUTCOME_RULES.get(event_type)
    if allowed is None or outcome not in allowed:
        raise InvalidInvocationTransitionError(
            f"{event_type.value} does not allow outcome {outcome.value}"
        )
    if event_type is ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED:
        if dispatch_status is not DispatchStatus.DISPATCHED:
            raise InvalidInvocationTransitionError(
                "Output guardrail rejection requires DISPATCHED"
            )
    if event_type is ModelInvocationEventType.SUCCEEDED:
        if dispatch_status is not DispatchStatus.DISPATCHED:
            raise InvalidInvocationTransitionError("Success requires DISPATCHED")
    if event_type is ModelInvocationEventType.VALIDATION_FAILED:
        if dispatch_status is not DispatchStatus.DISPATCHED:
            raise InvalidInvocationTransitionError(
                "Validation failure requires DISPATCHED"
            )
    if event_type is ModelInvocationEventType.OUTCOME_UNKNOWN:
        if dispatch_status is DispatchStatus.NOT_DISPATCHED:
            raise InvalidInvocationTransitionError(
                "Unknown outcome requires DISPATCHED or DISPATCH_UNKNOWN"
            )


def validate_error_fields(
    error_code: str | None,
    retry_reason: str | None,
) -> None:
    if error_code is not None and (
        type(error_code) is not str
        or _STABLE_CODE.fullmatch(error_code) is None
    ):
        raise ValueError("error_code must be a stable registered code")
    if retry_reason is not None and (
        type(retry_reason) is not str
        or retry_reason
        not in {
            "PROVIDER_RETRY",
            "OUTPUT_REPAIR",
            "GUARDRAIL_RETRY",
        }
    ):
        raise ValueError("retry_reason is not registered")




def sanitize_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    event_type: ModelInvocationEventType | str,
    schema_version: int = 1,
) -> dict[str, str]:
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError("metadata schema_version is not registered")
    try:
        registered_event = ModelInvocationEventType(event_type)
    except (TypeError, ValueError):
        raise ValueError("metadata event_type is not registered") from None
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    allowed = _EVENT_METADATA_KEYS[registered_event]
    required = _EVENT_METADATA_REQUIRED[registered_event]
    if len(metadata) > len(allowed):
        raise ValueError("metadata contains too many fields")
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in metadata.items():
        if not isinstance(raw_key, str) or raw_key != raw_key.strip():
            raise ValueError("metadata keys must be exact registered strings")
        key = raw_key
        if key not in allowed or _FORBIDDEN_METADATA_KEY.search(key):
            raise ValueError(
                f"metadata field is not registered for {registered_event.value}: {key}"
            )
        if type(raw_value) is not str:
            raise ValueError(f"metadata field must be a registered string: {key}")
        if key in _METADATA_ENUMS:
            if raw_value not in _METADATA_ENUMS[key]:
                raise ValueError(f"metadata value is not registered: {key}")
        elif not _STABLE_CODE.fullmatch(raw_value):
            raise ValueError(f"metadata value must be a stable code: {key}")
        cleaned[key] = raw_value
    missing = required - cleaned.keys()
    if missing:
        raise ValueError(
            "metadata required fields are missing: " + ", ".join(sorted(missing))
        )
    encoded = json.dumps(
        cleaned,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 1024:
        raise ValueError("metadata exceeds 1024 bytes")
    return cleaned


def model_metadata_registry_document() -> dict[str, Any]:
    events: dict[str, Any] = {}
    for event_type in ModelInvocationEventType:
        properties: dict[str, Any] = {}
        for key in sorted(_EVENT_METADATA_KEYS[event_type]):
            if key in _METADATA_ENUMS:
                properties[key] = {
                    "type": "string",
                    "enum": sorted(_METADATA_ENUMS[key]),
                }
            else:
                properties[key] = {
                    "type": "string",
                    "pattern": "^[A-Z][A-Z0-9_]{2,119}$",
                }
        events[event_type.value] = {
            "required": sorted(_EVENT_METADATA_REQUIRED[event_type]),
            "properties": properties,
            "additionalProperties": False,
        }
    return {
        "registry": "tuge.model-invocation-metadata",
        "schemaVersion": 1,
        "metadataObjectMaxBytes": 1024,
        "keyStyle": "snake_case",
        "topLevelFieldsNotMetadata": [
            "attemptNo",
            "fallbackFromInvocationId",
            "featureCode",
            "timeToFirstTokenMs",
        ],
        "events": events,
    }
