from __future__ import annotations

from .models import PipelineDefinition, validate_pipeline_definition


_PIPELINES: dict[str, PipelineDefinition] = {}
_PIPELINE_SOURCES: dict[str, str] = {}


def register_pipeline(definition: PipelineDefinition, *, source: str = "framework") -> None:
    validate_pipeline_definition(definition)
    existing = _PIPELINES.get(definition.pipeline_id)
    existing_source = _PIPELINE_SOURCES.get(definition.pipeline_id)
    if existing is not None and existing_source != source:
        raise ValueError(f"Pipeline '{definition.pipeline_id}' is already registered.")
    _PIPELINES[definition.pipeline_id] = definition
    _PIPELINE_SOURCES[definition.pipeline_id] = source


def get_pipeline_definition(pipeline_id: str) -> PipelineDefinition:
    try:
        return _PIPELINES[pipeline_id]
    except KeyError as exc:
        available = ", ".join(sorted(_PIPELINES))
        raise ValueError(f"Unknown pipeline '{pipeline_id}'. Available pipelines: {available}.") from exc


def list_pipeline_definitions() -> list[PipelineDefinition]:
    return list(_PIPELINES.values())


# Import built-in, business-neutral pipeline definitions after registry functions exist.
from . import demo as _demo  # noqa: E402,F401
from . import builtins as _builtins  # noqa: E402,F401
