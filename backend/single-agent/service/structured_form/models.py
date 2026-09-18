"""Domain-neutral definitions for AI-editable forms."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


FieldType = Literal["text", "date", "number", "enum"]


@dataclass(frozen=True, slots=True)
class FormFieldDefinition:
    key: str
    label: str
    aliases: tuple[str, ...] = ()
    field_type: FieldType = "text"
    enum_values: tuple[str, ...] = ()
    ai_writable: bool = True
    required: bool = False
    depends_on: str | None = None
    options_by_parent: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.label, *self.aliases)))


@dataclass(frozen=True, slots=True)
class FormWorkflowDefinition:
    workflow_type: str
    resource_type: str
    fields: tuple[FormFieldDefinition, ...] = field(default_factory=tuple)
    instructions: tuple[str, ...] = field(default_factory=tuple)
    assistant_mode: str = "TRAVEL_ASSISTANT"

    def writable_fields(self) -> tuple[FormFieldDefinition, ...]:
        return tuple(item for item in self.fields if item.ai_writable)
