"""Lightweight registry for task-injected tools."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Optional

from tool.bundle import ToolBundle

from .config import ToolProviderConfig


BundleFactory = Callable[[ToolProviderConfig], ToolBundle]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A concrete provider that can create one or more LLM tools."""

    tool_name: str
    provider: str
    display_name: str
    description: str
    factory: BundleFactory
    llm_tool_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> tuple[str, str]:
        return (self.tool_name, self.provider)


class ToolList:
    """Registry of tool providers available to task/API layers."""

    def __init__(self, definitions: Iterable[ToolDefinition] | None = None):
        self._definitions: dict[tuple[str, str], ToolDefinition] = {}
        self._default_provider: dict[str, str] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition, *, make_default: bool = False) -> None:
        self._definitions[definition.key] = definition
        if make_default or definition.tool_name not in self._default_provider:
            self._default_provider[definition.tool_name] = definition.provider

    def list(self) -> list[ToolDefinition]:
        return sorted(
            self._definitions.values(),
            key=lambda definition: (definition.tool_name, definition.provider),
        )

    def providers_for(self, tool_name: str) -> list[ToolDefinition]:
        return [
            definition
            for definition in self.list()
            if definition.tool_name == tool_name
        ]

    def get(self, tool_name: str, provider: Optional[str] = None) -> ToolDefinition:
        resolved_provider = provider or self._default_provider.get(tool_name)
        if not resolved_provider:
            raise KeyError(f"No provider registered for tool '{tool_name}'.")

        definition = self._definitions.get((tool_name, resolved_provider))
        if definition is None:
            raise KeyError(
                f"Tool provider '{tool_name}/{resolved_provider}' is not registered."
            )
        return definition

    def create_bundle(self, config: ToolProviderConfig) -> ToolBundle:
        if not config.enabled:
            return ToolBundle.empty()
        definition = self.get(config.tool_name, config.provider)
        return definition.factory(config)

