"""Deterministic fast path for explicit one-field edit commands."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import FormFieldDefinition
from .registry import get_workflow_definition


@dataclass(frozen=True, slots=True)
class FastFormChange:
    request_id: str
    draft_id: str
    expected_version: int
    field_key: str
    field_label: str
    value: Any

    def arguments(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "draft_id": self.draft_id,
            "expected_version": self.expected_version,
            "changes": [
                {"field_key": self.field_key, "value": self.value, "source": "ai"}
            ],
        }


def match_explicit_form_change(
    payload: dict[str, Any],
    message: str,
) -> FastFormChange | None:
    definition = get_workflow_definition(payload.get("active_workflow"))
    draft_id = str(payload.get("active_resource_id") or "").strip()
    version = payload.get("draft_version")
    if definition is None or not draft_id or not isinstance(version, int) or version < 1:
        return None

    normalized = str(message or "").strip()
    if not normalized or any(mark in normalized for mark in ("以及", "同时", "全部", "都改", "相关")):
        return None

    matches: list[tuple[FormFieldDefinition, str]] = []
    for field in definition.writable_fields():
        for alias in sorted(field.names, key=len, reverse=True):
            pattern = (
                rf"^(?:请)?(?:把|将)?\s*{re.escape(alias)}\s*"
                rf"(?:改成|修改为|设为|设置为|换成|调整为)\s*(.+?)\s*[。！!]?$"
            )
            match = re.match(pattern, normalized)
            if match:
                matches.append((field, match.group(1).strip()))
                break
    if len(matches) != 1:
        return None

    field, raw_value = matches[0]
    value = _normalize_value(field, raw_value)
    if value is None:
        return None
    return FastFormChange(
        request_id=f"fast-{uuid.uuid4().hex}",
        draft_id=draft_id,
        expected_version=version,
        field_key=field.key,
        field_label=field.label,
        value=value,
    )


def _normalize_value(field: FormFieldDefinition, raw_value: str) -> Any | None:
    if field.field_type == "date":
        return _normalize_date(raw_value)
    if field.field_type == "number":
        try:
            return format(Decimal(raw_value.replace(",", "").replace("元", "")), "f")
        except InvalidOperation:
            return None
    if field.field_type == "enum":
        compact = raw_value.strip()
        return compact if compact in field.enum_values else None
    return raw_value.strip() or None


def _normalize_date(raw_value: str) -> str | None:
    compact = raw_value.strip()
    iso = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", compact)
    if iso:
        year, month, day = map(int, iso.groups())
    else:
        short = re.fullmatch(r"(\d{1,2})月(\d{1,2})日?", compact)
        if not short:
            return None
        year = date.today().year
        month, day = map(int, short.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None
