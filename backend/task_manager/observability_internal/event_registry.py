from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from loguru import logger

from .schemas import MetadataField


class RegistryValidationError(ValueError):
    """Stable validation failure without retaining the rejected value."""


class MetadataType(StrEnum):
    STRING = "STRING"
    IDENTIFIER = "IDENTIFIER"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    STRING_LIST = "STRING_LIST"
    OBJECT = "OBJECT"
    ARRAY = "ARRAY"


class Sensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    MASKED = "MASKED"
    DROP = "DROP"


@dataclass(frozen=True, slots=True)
class MetadataRule:
    value_type: MetadataType
    max_length: int = 256
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    required: bool = False


@dataclass(frozen=True, slots=True)
class TaskEventDefinition:
    schema_version: int
    metadata: Mapping[str, MetadataRule]


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
JWT_PATTERN = re.compile(
    r"(?i)eyJ[A-Za-z0-9_-]{6,}\."
    r"[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
)
CAPABILITY_PATTERN = re.compile(
    r"(?i)(?:hwm_[A-Za-z0-9_-]{10,80}|"
    r"cap\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{43}|"
    r"qtk_qs(?:[._:-])?[A-Za-z0-9._~-]{8,}|"
    r"cur_[A-Za-z0-9_-]{8,}|"
    r"crf_revfin_v1(?:[._:-])?[A-Za-z0-9._~-]{8,}|"
    r"obs1(?:[._:-])?[A-Za-z0-9._~-]{8,}|"
    r"(?:cursor|retry|locator|accesscontext|highwatermark)"
    r"[._:-][A-Za-z0-9._~-]{8,})"
)
CREDENTIAL_MATERIAL_PATTERN = re.compile(
    r"(?i)(?:bearer(?:\s+|[:=._-]\s*)\S{4,}|"
    r"authorization\s*[:=]\s*\S{4,}|cookie\s*[:=]\s*\S{4,}|"
    r"(?:password|passwd|secret|api[_-]?key|credential)"
    r"\s*[:=._-]\s*\S{8,}|"
    r"(?<![A-Za-z0-9])(?:sk|ak)-[A-Za-z0-9_-]{6,})"
)
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "apikey",
    "api_key",
    "credential",
    "token",
    "prompt",
    "response",
    "contracttext",
    "contract_text",
    "documentcontent",
    "document_content",
    "highwatermark",
    "cursor",
    "locator",
    "accesscontext",
    "accesssession",
)
SENSITIVE_VALUE_FRAGMENTS = (
    "authorization",
    "bearer ",
    "cookie",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "token",
    "sk-",
    "ak-",
)


def _rule(
    value_type: MetadataType,
    *,
    max_length: int = 256,
    sensitivity: Sensitivity = Sensitivity.PUBLIC,
    required: bool = False,
) -> MetadataRule:
    return MetadataRule(value_type, max_length, sensitivity, required)


TASK_METADATA_RULES: dict[str, MetadataRule] = {
    "task_type": _rule(MetadataType.IDENTIFIER, max_length=120),
    "status": _rule(MetadataType.IDENTIFIER, max_length=32),
    "stage": _rule(MetadataType.IDENTIFIER, max_length=120),
    "step_id": _rule(MetadataType.IDENTIFIER, max_length=120),
    "step_index": _rule(MetadataType.INTEGER),
    "item_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "stage_run_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "agent_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "tool_name": _rule(MetadataType.IDENTIFIER, max_length=120),
    "handler_name": _rule(MetadataType.IDENTIFIER, max_length=80),
    "model_pack_id": _rule(MetadataType.IDENTIFIER, max_length=120),
    "model_id": _rule(MetadataType.IDENTIFIER, max_length=120),
    "skill_package": _rule(MetadataType.IDENTIFIER, max_length=160),
    "primary_skill": _rule(MetadataType.IDENTIFIER, max_length=160),
    "candidate_skills": _rule(MetadataType.STRING_LIST, max_length=160),
    "extra_tools": _rule(MetadataType.STRING_LIST, max_length=160),
    "extra_datasets": _rule(MetadataType.STRING_LIST, max_length=160),
    "source_event": _rule(MetadataType.IDENTIFIER, max_length=64),
    "content_chars": _rule(MetadataType.INTEGER),
    "tool_call_id": _rule(MetadataType.IDENTIFIER, max_length=120),
    "attempt_count": _rule(MetadataType.INTEGER),
    "attempt": _rule(MetadataType.INTEGER),
    "next_attempt": _rule(MetadataType.INTEGER),
    "retryable": _rule(MetadataType.BOOLEAN),
    "result_chars": _rule(MetadataType.INTEGER),
    "artifact_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "artifact_type": _rule(MetadataType.IDENTIFIER, max_length=120),
    "artifact_version": _rule(MetadataType.INTEGER),
    "run_status": _rule(MetadataType.IDENTIFIER, max_length=32),
    "run_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "item_count": _rule(MetadataType.INTEGER),
    "resource_count": _rule(MetadataType.INTEGER),
    "batch_size": _rule(MetadataType.INTEGER),
    "max_concurrency": _rule(MetadataType.INTEGER),
    "retry_per_item": _rule(MetadataType.INTEGER),
    "progress_current": _rule(MetadataType.INTEGER),
    "progress_total": _rule(MetadataType.INTEGER),
    "count": _rule(MetadataType.INTEGER),
    "created": _rule(MetadataType.INTEGER),
    "stage_index": _rule(MetadataType.INTEGER),
    "succeeded": _rule(MetadataType.INTEGER),
    "failed": _rule(MetadataType.INTEGER),
    "skipped": _rule(MetadataType.INTEGER),
    "total": _rule(MetadataType.INTEGER),
    "completed": _rule(MetadataType.INTEGER),
    "index": _rule(MetadataType.INTEGER),
    "backoff_seconds": _rule(MetadataType.NUMBER),
    "failure_policy": _rule(MetadataType.IDENTIFIER, max_length=64),
    "error_code": _rule(MetadataType.IDENTIFIER, max_length=120),
    "has_result": _rule(MetadataType.BOOLEAN),
    "has_structured_output": _rule(MetadataType.BOOLEAN),
    "resume_from_stage": _rule(MetadataType.IDENTIFIER, max_length=120),
    "pipeline_id": _rule(MetadataType.IDENTIFIER, max_length=120),
    "version": _rule(MetadataType.IDENTIFIER, max_length=80),
    "stage_type": _rule(MetadataType.IDENTIFIER, max_length=80),
    "output_schema_name": _rule(MetadataType.IDENTIFIER, max_length=120),
    "final_artifact_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "review_artifact_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "checksum": _rule(MetadataType.IDENTIFIER, max_length=160),
    "outcome": _rule(MetadataType.IDENTIFIER, max_length=32),
    "tool": _rule(MetadataType.IDENTIFIER, max_length=120),
    "item_type": _rule(MetadataType.IDENTIFIER, max_length=80),
    "action": _rule(MetadataType.IDENTIFIER, max_length=64),
    "thread_id": _rule(MetadataType.IDENTIFIER, max_length=80),
    "session_id": _rule(MetadataType.IDENTIFIER, max_length=160),
    "task_memory_version": _rule(MetadataType.INTEGER),
    # Known business payload fields are validated but never projected.
    "item_key": _rule(
        MetadataType.STRING, max_length=160, sensitivity=Sensitivity.DROP
    ),
    "parser": _rule(MetadataType.STRING, max_length=4000, sensitivity=Sensitivity.DROP),
    "synced_items": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "result": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "original_chars": _rule(MetadataType.INTEGER, sensitivity=Sensitivity.DROP),
    "preview": _rule(
        MetadataType.STRING, max_length=200_000, sensitivity=Sensitivity.DROP
    ),
    "truncated": _rule(MetadataType.BOOLEAN, sensitivity=Sensitivity.DROP),
    "delta": _rule(
        MetadataType.STRING, max_length=200_000, sensitivity=Sensitivity.DROP
    ),
    "arguments": _rule(
        MetadataType.STRING, max_length=200_000, sensitivity=Sensitivity.DROP
    ),
    "artifacts": _rule(MetadataType.ARRAY, sensitivity=Sensitivity.DROP),
    "usage": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "patch": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "content": _rule(
        MetadataType.STRING, max_length=200_000, sensitivity=Sensitivity.DROP
    ),
    "output": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "data": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "error": _rule(MetadataType.STRING, max_length=4000, sensitivity=Sensitivity.DROP),
    "error_message": _rule(
        MetadataType.STRING, max_length=4000, sensitivity=Sensitivity.DROP
    ),
    "message": _rule(
        MetadataType.STRING, max_length=4000, sensitivity=Sensitivity.DROP
    ),
    "reason": _rule(MetadataType.STRING, max_length=4000, sensitivity=Sensitivity.DROP),
    "comment": _rule(
        MetadataType.STRING, max_length=8000, sensitivity=Sensitivity.DROP
    ),
    "pause_payload": _rule(MetadataType.OBJECT, sensitivity=Sensitivity.DROP),
    "allowed_actions": _rule(MetadataType.ARRAY, sensitivity=Sensitivity.DROP),
    "reason_codes": _rule(MetadataType.ARRAY, sensitivity=Sensitivity.DROP),
    "type": _rule(MetadataType.IDENTIFIER, max_length=120),
}


def _event_definition(
    allowed: Iterable[str],
    *,
    required: Iterable[str] = (),
) -> TaskEventDefinition:
    allowed_keys = tuple(allowed)
    required_keys = frozenset(required)
    if required_keys - set(allowed_keys):
        raise RuntimeError("Task Event registry required keys must be allowed.")
    metadata: dict[str, MetadataRule] = {}
    for key in allowed_keys:
        source = TASK_METADATA_RULES[key]
        metadata[key] = MetadataRule(
            value_type=source.value_type,
            max_length=source.max_length,
            sensitivity=source.sensitivity,
            required=key in required_keys,
        )
    return TaskEventDefinition(schema_version=1, metadata=metadata)


_BATCH_SUMMARY = ("total", "succeeded", "failed", "skipped")
_TOOL_FIELDS = (
    "tool",
    "tool_name",
    "tool_call_id",
    "status",
    "attempt",
    "retryable",
    "arguments",
    "result_chars",
    "has_result",
    "artifacts",
    "error",
)

TASK_EVENT_REGISTRY: dict[tuple[str, int], TaskEventDefinition] = {
    ("task_created", 1): _event_definition(
        ("task_type", "agent_id", "handler_name", "model_pack_id", "item_count"),
        required=("task_type",),
    ),
    ("task_started", 1): _event_definition(("task_type", "attempt_count", "status")),
    ("task_succeeded", 1): _event_definition(
        ("status", "result", "has_structured_output", "synced_items")
    ),
    ("task_failed", 1): _event_definition(
        ("type", "stage", "message", "retryable", "status", "error_code")
    ),
    ("task_cancelled", 1): _event_definition(("run_id", "status")),
    ("scheduler_request_built", 1): _event_definition(
        (
            "agent_id",
            "model_id",
            "skill_package",
            "primary_skill",
            "candidate_skills",
            "extra_tools",
            "extra_datasets",
            "task_memory_version",
        ),
        required=("agent_id",),
    ),
    ("agent_metadata", 1): _event_definition(
        ("source_event", "agent_id"), required=("source_event", "agent_id")
    ),
    ("agent_final", 1): _event_definition(
        ("source_event", "content_chars", "usage", "artifacts"),
        required=("source_event", "content_chars"),
    ),
    ("stream_chunk", 1): _event_definition(
        (
            "source_event",
            "status",
            "content_chars",
            "delta",
            "content",
            "arguments",
            "result",
        )
    ),
    ("tool_started", 1): _event_definition(_TOOL_FIELDS),
    ("tool_completed", 1): _event_definition(_TOOL_FIELDS),
    ("tool_failed", 1): _event_definition(_TOOL_FIELDS),
    ("batch_started", 1): _event_definition(
        ("item_count", "max_concurrency", "failure_policy", "retry_per_item"),
        required=("item_count", "max_concurrency", "failure_policy", "retry_per_item"),
    ),
    ("batch_succeeded", 1): _event_definition(_BATCH_SUMMARY),
    ("batch_failed", 1): _event_definition(_BATCH_SUMMARY),
    ("item_started", 1): _event_definition(
        ("item_key", "item_type", "attempt", "skill_package"),
        required=("item_key", "item_type", "attempt"),
    ),
    ("item_output_parse_failed", 1): _event_definition(
        ("item_key", "parser"), required=("item_key", "parser")
    ),
    ("item_output_validation_failed", 1): _event_definition(
        ("item_key",), required=("item_key",)
    ),
    ("item_succeeded", 1): _event_definition(
        ("item_key", "progress_current", "progress_total"),
        required=("item_key", "progress_current", "progress_total"),
    ),
    ("item_retry", 1): _event_definition(
        ("item_key", "attempt"), required=("item_key", "attempt")
    ),
    ("item_failed", 1): _event_definition(
        ("item_key", "progress_current", "progress_total"),
        required=("item_key", "progress_current", "progress_total"),
    ),
    ("item_stream_chunk", 1): _event_definition(
        ("item_key", "delta"), required=("item_key", "delta")
    ),
    ("output_parse_failed", 1): _event_definition(("output_schema_name", "parser")),
    ("output_validation_failed", 1): _event_definition(()),
    ("human_review_submitted", 1): _event_definition(
        ("action", "comment", "patch", "resume_from_stage", "artifact_id"),
        required=("action", "artifact_id"),
    ),
    ("human_review_required", 1): _event_definition(
        (
            "review_artifact_id",
            "reason",
            "reason_codes",
            "allowed_actions",
            "pause_payload",
            "action",
            "comment",
            "patch",
        )
    ),
    ("pipeline_resumed", 1): _event_definition(("resume_from_stage",)),
    ("pipeline_started", 1): _event_definition(
        ("pipeline_id", "version"), required=("pipeline_id", "version")
    ),
    ("pipeline_completed", 1): _event_definition(
        ("final_artifact_id",), required=("final_artifact_id",)
    ),
    ("pipeline_paused", 1): _event_definition(
        ("reason", "reason_codes", "allowed_actions", "pause_payload")
    ),
    ("stage_started", 1): _event_definition(
        ("stage_type", "attempt", "stage_index", "status", "stage", "stage_run_id")
    ),
    ("stage_failed", 1): _event_definition(("attempt", "error_code")),
    ("stage_retrying", 1): _event_definition(
        ("next_attempt", "backoff_seconds"),
        required=("next_attempt", "backoff_seconds"),
    ),
    ("stage_result_sink_failed", 1): _event_definition(()),
    ("stage_skipped", 1): _event_definition(
        ("failure_policy", "error_message"), required=("failure_policy",)
    ),
    ("stage_completed", 1): _event_definition(
        ("artifact_id", "artifact_type", "status")
    ),
    ("artifact_created", 1): _event_definition(
        ("artifact_id", "artifact_type", "artifact_version", "checksum"),
        required=("artifact_id", "artifact_type", "artifact_version", "checksum"),
    ),
    # Schema v1 exists in two producer shapes. Both carry result content that must
    # never be projected; accepting the legacy envelope keeps one historical row
    # from making the complete event page unavailable.
    ("result_snapshot", 1): _event_definition(
        ("artifact_id", "result", "original_chars", "preview", "truncated")
    ),
    ("direct_model_started", 1): _event_definition(
        ("model_id", "skill_package"), required=("model_id", "skill_package")
    ),
    ("direct_model_completed", 1): _event_definition(()),
    ("batch_stage_completed", 1): _event_definition(()),
    ("agent_started", 1): _event_definition(
        ("thread_id", "session_id", "task_memory_version"),
    ),
    ("agent_completed", 1): _event_definition(("usage",)),
    ("agent_delta", 1): _event_definition(("delta",)),
}

_TASK_EVENT_UNKNOWN_METADATA_DROPS: Counter[tuple[str, int]] = Counter()

TASK_TOKEN_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "input_cached_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)

TASK_TOKEN_USAGE_DROP_KEYS = frozenset(
    {
        "completion_tokens_details",
        "prompt_tokens_details",
    }
)
SECURITY_METADATA_REGISTRY: dict[str, MetadataRule] = {
    "authentication_method": _rule(MetadataType.IDENTIFIER, max_length=80),
    "authorization_decision": _rule(MetadataType.IDENTIFIER, max_length=80),
    "decision_result": _rule(MetadataType.IDENTIFIER, max_length=32, required=True),
    "failure_stage": _rule(MetadataType.IDENTIFIER, max_length=80),
    "policy_id": _rule(MetadataType.IDENTIFIER, max_length=120),
    "policy_version": _rule(MetadataType.IDENTIFIER, max_length=80),
    "query_kind": _rule(MetadataType.IDENTIFIER, max_length=80),
    "resource_count": _rule(MetadataType.INTEGER),
    "resource_type": _rule(MetadataType.IDENTIFIER, max_length=80),
    "retryable": _rule(MetadataType.BOOLEAN),
    "scope_kind": _rule(MetadataType.IDENTIFIER, max_length=32),
    "service_identity": _rule(MetadataType.IDENTIFIER, max_length=120),
    "transport": _rule(MetadataType.IDENTIFIER, max_length=32),
}

TASK_EVENT_SOURCE_FIELDS: dict[str, frozenset[str]] = {
    "agent": frozenset({"type", "id"}),
    "artifact": frozenset({"type", "id"}),
    "external_executor": frozenset({"type", "id"}),
    "human": frozenset({"type", "id"}),
    "pipeline": frozenset({"type", "id"}),
    "task_manager": frozenset({"type", "id"}),
    "tool": frozenset({"type", "id", "name", "tool_name", "toolName"}),
}
TASK_EVENT_SOURCE_LIMITS = {
    "type": 32,
    "id": 120,
    "name": 120,
    "tool_name": 120,
    "toolName": 120,
}


def register_task_event_type(
    event_type: str,
    *,
    schema_version: int,
    metadata: Mapping[str, MetadataRule],
) -> None:
    """Explicit extension point used by a reviewed feature registration."""

    normalized = _identifier(event_type, maximum=80)
    registry_key = (normalized, schema_version)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
        or registry_key in TASK_EVENT_REGISTRY
    ):
        raise RegistryValidationError("task event registration conflicts")
    for key, rule in metadata.items():
        _identifier(key, maximum=64)
        if not isinstance(rule, MetadataRule):
            raise RegistryValidationError("task event metadata rule is invalid")
    TASK_EVENT_REGISTRY[registry_key] = TaskEventDefinition(
        schema_version, dict(metadata)
    )


def task_metadata_fields(
    event_type: str,
    schema_version: int,
    payload: Mapping[str, Any] | None,
) -> list[MetadataField]:
    definition = _task_event_definition(event_type, schema_version)
    if payload is not None and not isinstance(payload, Mapping):
        raise RegistryValidationError("task event payload is not an object")
    values = dict(payload or {})
    if any(not isinstance(key, str) for key in values):
        raise RegistryValidationError("task event metadata key is invalid")
    unknown = set(values) - set(definition.metadata)
    if any(
        _is_sensitive_key(key)
        or _unknown_metadata_contains_sensitive_material(values[key])
        for key in unknown
    ):
        raise RegistryValidationError("task event metadata key is not allowed")
    if unknown:
        _record_unknown_metadata_drop(event_type, schema_version)
    missing = {
        key
        for key, rule in definition.metadata.items()
        if rule.required and (key not in values or values[key] is None)
    }
    if missing:
        raise RegistryValidationError("task event metadata is incomplete")
    result: list[MetadataField] = []
    for key in sorted(set(values) & set(definition.metadata)):
        rule = definition.metadata[key]
        value = values[key]
        if value is None or (
            isinstance(value, str) and not value.strip() and not rule.required
        ):
            continue
        rendered = _validate_and_render(value, rule)
        if rule.sensitivity == Sensitivity.DROP:
            continue
        result.append(
            MetadataField(
                key=key,
                value=rendered,
                masked=rule.sensitivity == Sensitivity.MASKED,
            )
        )
    return result


def sanitize_task_event_payload(
    event_type: str,
    schema_version: int,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the only Task Event metadata allowed in the authority ledger."""

    definition = _task_event_definition(event_type, schema_version)
    if payload is not None and not isinstance(payload, Mapping):
        raise RegistryValidationError("task event payload is not an object")
    values = dict(payload or {})
    if any(not isinstance(key, str) for key in values):
        raise RegistryValidationError("task event metadata key is invalid")
    unknown = set(values) - set(definition.metadata)
    if any(
        _is_sensitive_key(key)
        or _unknown_metadata_contains_sensitive_material(values[key])
        for key in unknown
    ):
        raise RegistryValidationError("task event metadata key is not allowed")
    if unknown:
        _record_unknown_metadata_drop(event_type, schema_version)
    missing = {
        key
        for key, rule in definition.metadata.items()
        if rule.required and (key not in values or values[key] is None)
    }
    if missing:
        raise RegistryValidationError("task event metadata is incomplete")

    result: dict[str, Any] = {}
    for key in sorted(values):
        rule = definition.metadata.get(key)
        if rule is None or rule.sensitivity == Sensitivity.DROP:
            continue
        value = values[key]
        if value is None or (
            isinstance(value, str) and not value.strip() and not rule.required
        ):
            continue
        result[key] = _validate_for_storage(value, rule)
    return result


def sanitize_task_event_token_usage(
    token_usage: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Keep only non-negative aggregate token counters, never provider payloads."""

    if token_usage is not None and not isinstance(token_usage, Mapping):
        raise RegistryValidationError("task event token usage is not an object")
    values = dict(token_usage or {})
    if any(not isinstance(key, str) for key in values):
        raise RegistryValidationError("task event token usage key is invalid")
    unknown = set(values) - TASK_TOKEN_USAGE_KEYS - TASK_TOKEN_USAGE_DROP_KEYS
    if any(_is_sensitive_key(key) for key in unknown):
        raise RegistryValidationError("task event token usage key is not allowed")
    result: dict[str, int] = {}
    for key in sorted(TASK_TOKEN_USAGE_KEYS & set(values)):
        value = values[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RegistryValidationError("task event token usage value is invalid")
        result[key] = value
    return result


def task_event_display_message(event_type: str, schema_version: int) -> str:
    """Derive a stable message without persisting producer-controlled text."""

    _task_event_definition(event_type, schema_version)
    return event_type.replace("_", " ").capitalize() + "."


def sanitize_task_event_source(
    source: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Validate source_json as identifiers, never as producer-controlled prose."""

    if source is None:
        return {}
    if not isinstance(source, Mapping) or any(
        not isinstance(key, str) for key in source
    ):
        raise RegistryValidationError("task event source is invalid")
    values = dict(source)
    if not values:
        return {}
    if any(_is_sensitive_key(key) for key in values):
        raise RegistryValidationError("task event source key is not allowed")
    source_type = _source_identifier(values.get("type"), maximum=32)
    allowed = TASK_EVENT_SOURCE_FIELDS.get(source_type)
    if allowed is None or set(values) - allowed:
        raise RegistryValidationError("task event source is not registered")
    result = {"type": source_type}
    for key in sorted(set(values) - {"type"}):
        value = values[key]
        if value is None:
            continue
        result[key] = _source_identifier(value, maximum=TASK_EVENT_SOURCE_LIMITS[key])
    if source_type == "tool" and not any(
        result.get(key) for key in ("id", "name", "tool_name", "toolName")
    ):
        raise RegistryValidationError("task event tool source has no identifier")
    return result


def security_metadata_fields(
    items: Iterable[Mapping[str, Any]] | None,
    *,
    require_decision_result: bool,
) -> list[dict[str, Any]]:
    values: dict[str, tuple[Any, bool]] = {}
    for item in items or ():
        if not isinstance(item, Mapping):
            raise RegistryValidationError("security metadata item is not an object")
        if set(item) - {"key", "value", "masked"}:
            raise RegistryValidationError("security metadata item has unknown fields")
        key = item.get("key")
        if not isinstance(key, str):
            raise RegistryValidationError("security metadata key is invalid")
        key = key.strip()
        if key in values:
            raise RegistryValidationError("security metadata key is duplicated")
        rule = SECURITY_METADATA_REGISTRY.get(key)
        if rule is None or _is_sensitive_key(key):
            raise RegistryValidationError("security metadata key is not registered")
        masked = item.get("masked", False)
        if not isinstance(masked, bool):
            raise RegistryValidationError("security metadata masked flag is invalid")
        values[key] = (item.get("value"), masked)
    required = {
        key
        for key, rule in SECURITY_METADATA_REGISTRY.items()
        if rule.required and require_decision_result
    }
    if required - set(values):
        raise RegistryValidationError("security metadata is incomplete")
    result: list[dict[str, Any]] = []
    for key in sorted(values):
        raw, requested_mask = values[key]
        rule = SECURITY_METADATA_REGISTRY[key]
        rendered = _validate_and_render(raw, rule)
        result.append(
            {
                "key": key,
                "value": rendered,
                "masked": requested_mask or rule.sensitivity == Sensitivity.MASKED,
            }
        )
    return result


def _task_event_definition(event_type: str, schema_version: int) -> TaskEventDefinition:
    definition = TASK_EVENT_REGISTRY.get((event_type, schema_version))
    if definition is None:
        raise RegistryValidationError("task event is not registered")
    return definition


def _record_unknown_metadata_drop(event_type: str, schema_version: int) -> None:
    _TASK_EVENT_UNKNOWN_METADATA_DROPS[(event_type, schema_version)] += 1
    logger.warning(
        "Task Event metadata dropped; "
        "code=TASK_EVENT_UNKNOWN_METADATA_DROPPED "
        "event_type={} schema_version={}",
        event_type,
        schema_version,
    )


def task_event_unknown_metadata_drop_count(
    event_type: str,
    schema_version: int,
) -> int:
    """Low-cardinality counter boundary for the metrics adapter."""

    return _TASK_EVENT_UNKNOWN_METADATA_DROPS[(event_type, schema_version)]


def _unknown_metadata_contains_sensitive_material(value: Any) -> bool:
    if isinstance(value, str):
        return contains_sensitive_material(value)
    if isinstance(value, Mapping):
        return any(
            isinstance(key, str)
            and _is_sensitive_key(key)
            or _unknown_metadata_contains_sensitive_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _unknown_metadata_contains_sensitive_material(item) for item in value
        )
    return False


def _validate_for_storage(value: Any, rule: MetadataRule) -> Any:
    rendered = _validate_and_render(value, rule)
    if rule.sensitivity == Sensitivity.MASKED:
        return "[MASKED]"
    if rule.value_type in {MetadataType.STRING, MetadataType.IDENTIFIER}:
        return rendered
    if rule.value_type == MetadataType.INTEGER:
        return int(rendered)
    if rule.value_type == MetadataType.NUMBER:
        return value
    if rule.value_type == MetadataType.BOOLEAN:
        return rendered == "true"
    if rule.value_type == MetadataType.STRING_LIST:
        return [item.strip() for item in value]
    raise RegistryValidationError("task event metadata type cannot be persisted")


def _validate_and_render(value: Any, rule: MetadataRule) -> str:
    value_type = rule.value_type
    if rule.sensitivity == Sensitivity.DROP and value_type == MetadataType.STRING:
        # Dropped producer content is never returned to Java. Validate only its
        # outer type and storage bound; whitespace and line breaks are normal in
        # streaming deltas and must not invalidate the complete event page.
        if not isinstance(value, str) or len(value) > rule.max_length:
            raise RegistryValidationError("metadata string is invalid")
        return ""
    if value_type in {MetadataType.STRING, MetadataType.IDENTIFIER}:
        if not isinstance(value, str):
            raise RegistryValidationError("metadata value type is invalid")
        rendered = value.strip()
        if not rendered or len(rendered) > rule.max_length or _has_control(rendered):
            raise RegistryValidationError("metadata string is invalid")
        if value_type == MetadataType.IDENTIFIER and not IDENTIFIER_PATTERN.fullmatch(
            rendered
        ):
            raise RegistryValidationError("metadata identifier is invalid")
    elif value_type == MetadataType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RegistryValidationError("metadata integer is invalid")
        rendered = str(value)
    elif value_type == MetadataType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RegistryValidationError("metadata number is invalid")
        if not math.isfinite(float(value)) or float(value) < 0:
            raise RegistryValidationError("metadata number is invalid")
        rendered = str(value)
    elif value_type == MetadataType.BOOLEAN:
        if not isinstance(value, bool):
            raise RegistryValidationError("metadata boolean is invalid")
        rendered = "true" if value else "false"
    elif value_type == MetadataType.STRING_LIST:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RegistryValidationError("metadata string list is invalid")
        if any(
            not item.strip()
            or len(item.strip()) > rule.max_length
            or _has_control(item)
            for item in value
        ):
            raise RegistryValidationError("metadata string list is invalid")
        rendered = ",".join(item.strip() for item in value)
    elif value_type == MetadataType.OBJECT:
        if not isinstance(value, Mapping):
            raise RegistryValidationError("metadata object is invalid")
        rendered = ""
    elif value_type == MetadataType.ARRAY:
        if not isinstance(value, list):
            raise RegistryValidationError("metadata array is invalid")
        rendered = ""
    else:
        raise RegistryValidationError("metadata registry type is invalid")

    if rule.sensitivity != Sensitivity.DROP and _contains_sensitive_value(rendered):
        raise RegistryValidationError("metadata contains a sensitive capability")
    return rendered


def _identifier(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise RegistryValidationError("identifier must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or not IDENTIFIER_PATTERN.fullmatch(normalized)
    ):
        raise RegistryValidationError("identifier is invalid")
    return normalized


def _source_identifier(value: object, *, maximum: int) -> str:
    normalized = _identifier(value, maximum=maximum)
    if contains_sensitive_material(normalized):
        raise RegistryValidationError("task event source identifier is invalid")
    return normalized


def _is_sensitive_key(key: str) -> bool:
    folded = key.casefold().replace("-", "").replace("_", "")
    return any(
        fragment.replace("-", "").replace("_", "") in folded
        for fragment in SENSITIVE_KEY_FRAGMENTS
    )


def contains_capability_material(value: str) -> bool:
    return bool(JWT_PATTERN.search(value)) or bool(CAPABILITY_PATTERN.search(value))


def contains_sensitive_material(value: str) -> bool:
    return contains_capability_material(value) or bool(
        CREDENTIAL_MATERIAL_PATTERN.search(value)
    )


def _contains_sensitive_value(value: str) -> bool:
    folded = value.casefold()
    return any(
        fragment in folded for fragment in SENSITIVE_VALUE_FRAGMENTS
    ) or contains_sensitive_material(value)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
