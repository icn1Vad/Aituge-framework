from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .event_registry import (
    RegistryValidationError,
    security_metadata_fields,
    task_metadata_fields,
)
from .schemas import MetadataField

HMAC_IP_PATTERN = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
MASKED_IPV4_PATTERN = re.compile(
    r"^ipv4:(?P<first>[0-9]{1,3})\.(?P<second>[0-9]{1,3})\.\*\.\*$"
)
MASKED_IPV6_PATTERN = re.compile(
    r"^ipv6:(?P<prefix>[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,3}):\*$"
)


def metadata_fields(
    payload: Mapping[str, Any] | None,
    *,
    event_type: str,
    schema_version: int,
) -> list[MetadataField]:
    return task_metadata_fields(event_type, schema_version, payload)


def sanitized_security_metadata(
    items: Iterable[Mapping[str, Any]] | None,
    *,
    require_decision_result: bool = False,
) -> list[dict[str, Any]]:
    return security_metadata_fields(
        items,
        require_decision_result=require_decision_result,
    )


def normalize_source_ip_masked(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryValidationError("source IP summary must be a string")
    normalized = value.strip().lower()
    if HMAC_IP_PATTERN.fullmatch(normalized):
        return normalized
    ipv4 = MASKED_IPV4_PATTERN.fullmatch(normalized)
    if ipv4 and all(0 <= int(ipv4.group(name)) <= 255 for name in ("first", "second")):
        return normalized
    if MASKED_IPV6_PATTERN.fullmatch(normalized):
        return normalized
    raise RegistryValidationError("source IP summary format is invalid")
