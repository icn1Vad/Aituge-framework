"""Mount one trusted service capability entry into framework registries."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from llama_index.core.tools import FunctionTool
from loguru import logger
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from scheduling.agent_registry import get_agent_profile, upsert_agent_profile
from skill import register_skill_root, upsert_skill_package
from skill.package_models import SkillPackageEntity
from task_manager import TaskType, register_task_definition
from task_manager.payload_schemas import register_input_schema, register_output_schema
from task_manager.pipeline.models import (
    AgentStageConfig,
    BatchStageConfig,
    PipelineDefinition,
    RetryPolicy,
    StageDefinition,
)
from task_manager.pipeline.registry import register_pipeline as register_pipeline_definition
from tool import ToolBundle
from tool.registry import (
    ToolConfigEntity,
    ToolDefinition,
    ToolProviderConfig,
    get_default_tool_list,
)


CAPABILITY_ENTRY_ENV = "AITUGE_CAPABILITY_ENTRY"
CAPABILITY_ENTRIES_ENV = "AITUGE_CAPABILITY_ENTRIES"
_CAPABILITY_MARKER = "_capability_id"
_TOOL_SOURCES: dict[tuple[str, str], str] = {}


@dataclass(frozen=True, slots=True)
class CapabilitySettings:
    """Read-only environment view passed to a mounted capability."""

    values: Mapping[str, str]

    def get(self, name: str, default: str = "") -> str:
        return str(self.values.get(name, default))

    def require(self, name: str) -> str:
        value = self.get(name).strip()
        if not value:
            raise ValueError(f"Required capability setting '{name}' is not configured.")
        return value


@dataclass(frozen=True, slots=True)
class _AgentRegistration:
    agent_id: str
    name: str
    description: str
    agent_type: str
    model_id: str
    system_prompt: str
    default_tools: tuple[str, ...]
    default_datasets: tuple[str, ...]
    runtime_config: Mapping[str, Any]
    enabled: bool


@dataclass(frozen=True, slots=True)
class _SkillPackageRegistration:
    package_name: str
    display_name: str
    description: str
    tags: tuple[str, ...]
    primary_skill: str
    auxiliary_skills: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True, slots=True)
class _HttpToolRegistration:
    tool_name: str
    provider: str
    display_name: str
    description: str
    base_url: str
    path: str
    method: str
    input_model: type[BaseModel]
    timeout_seconds: float
    max_response_chars: int

    @property
    def key(self) -> tuple[str, str]:
        return self.tool_name, self.provider


class CapabilityRegistry:
    """Restricted declaration surface exposed to one trusted service entry."""

    def __init__(self, *, source_id: str) -> None:
        normalized = source_id.strip()
        if not normalized:
            raise ValueError("Capability source id is required.")
        self.source_id = normalized
        self._tasks: dict[str, TaskType] = {}
        self._pipelines: dict[str, PipelineDefinition] = {}
        self._agents: dict[str, _AgentRegistration] = {}
        self._skill_packages: dict[str, _SkillPackageRegistration] = {}
        self._skill_roots: list[Path] = []
        self._http_tools: dict[tuple[str, str], _HttpToolRegistration] = {}
        self._input_schemas: dict[str, type[BaseModel]] = {}
        self._output_schemas: dict[str, type[BaseModel]] = {}

    def register_task(
        self,
        *,
        task_type: str,
        name: str,
        description: str = "",
        handler: str = "scheduler",
        default_agent_id: str,
        default_skill_package: str | None = None,
        default_primary_skill: str | None = None,
        default_tools: list[str] | None = None,
        default_datasets: list[str] | None = None,
        stream_chunk_chars: int = 400,
        conversation_message_field: str | None = None,
        input_model: type[BaseModel] | None = None,
        output_model: type[BaseModel] | None = None,
        item_output_model: type[BaseModel] | None = None,
        result_sink_url: str | None = None,
        pipeline_id: str | None = None,
    ) -> None:
        normalized = task_type.strip()
        if not normalized:
            raise ValueError("Capability task_type is required.")
        if normalized in self._tasks:
            raise ValueError(f"Task type '{normalized}' is declared more than once.")
        if not 1 <= int(stream_chunk_chars) <= 4000:
            raise ValueError("Task stream_chunk_chars must be between 1 and 4000.")
        for label, model in (
            ("input_model", input_model),
            ("output_model", output_model),
            ("item_output_model", item_output_model),
        ):
            if model is not None and (not isinstance(model, type) or not issubclass(model, BaseModel)):
                raise ValueError(f"Capability task {label} must be a Pydantic model.")
        normalized_message_field = (conversation_message_field or "").strip() or None
        normalized_handler = handler.strip() or "scheduler"
        normalized_pipeline_id = (pipeline_id or "").strip() or None
        if normalized_handler == "pipeline" and not normalized_pipeline_id:
            raise ValueError("Capability pipeline tasks require pipeline_id.")
        if normalized_handler != "pipeline" and normalized_pipeline_id:
            raise ValueError("Capability pipeline_id is only valid for the pipeline handler.")
        if normalized_message_field:
            if normalized_handler != "scheduler":
                raise ValueError(
                    "Capability task conversation_message_field is only supported by "
                    "the scheduler handler."
                )
            if input_model is None:
                raise ValueError(
                    "Capability task conversation_message_field requires an input_model."
                )
            if normalized_message_field not in input_model.model_fields:
                raise ValueError(
                    f"Capability task conversation_message_field '{normalized_message_field}' "
                    "does not exist in input_model."
                )
        normalized_sink = (result_sink_url or "").strip() or None
        if normalized_sink:
            parsed = urlsplit(normalized_sink)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Capability task result_sink_url must be an http(s) URL.")
        schema_prefix = _schema_prefix(self.source_id, normalized)
        input_schema_name = f"{schema_prefix}_input" if input_model else None
        output_schema_name = f"{schema_prefix}_output" if output_model else None
        item_output_schema_name = f"{schema_prefix}_item_output" if item_output_model else None
        if input_model:
            self._input_schemas[input_schema_name] = input_model
        if output_model:
            self._output_schemas[output_schema_name] = output_model
        if item_output_model:
            self._output_schemas[item_output_schema_name] = item_output_model
        self._tasks[normalized] = TaskType(
            task_type=normalized,
            name=name.strip() or normalized,
            description=description.strip(),
            handler=normalized_handler,
            default_agent_id=default_agent_id.strip(),
            default_skill_package=(default_skill_package or "").strip() or None,
            default_primary_skill=(default_primary_skill or "").strip() or None,
            default_candidate_skills=[],
            default_tools=_dedupe(default_tools or []),
            default_datasets=_dedupe(default_datasets or []),
            input_schema_name=input_schema_name,
            output_schema_name=output_schema_name,
            item_output_schema_name=item_output_schema_name,
            result_sink_url=normalized_sink,
            pipeline_id=normalized_pipeline_id,
            stream_chunk_chars=int(stream_chunk_chars),
            conversation_message_field=normalized_message_field,
        )

    def register_pipeline(
        self,
        *,
        pipeline_id: str,
        version: str,
        task_type: str,
        stages: list[Mapping[str, Any]],
        description: str = "",
        final_artifact_type: str = "pipeline_result",
        timeout_seconds: int = 900,
        resumable: bool = True,
        max_parallelism: int = 1,
    ) -> None:
        normalized_id = pipeline_id.strip()
        normalized_task_type = task_type.strip()
        if not normalized_id or not normalized_task_type:
            raise ValueError("Capability pipeline id and task_type are required.")
        if normalized_id in self._pipelines:
            raise ValueError(f"Pipeline '{normalized_id}' is declared more than once.")

        definitions: list[StageDefinition] = []
        for raw in stages:
            stage_id = str(raw.get("stage_id") or "").strip()
            stage_type = str(raw.get("stage_type") or "").strip()
            if not stage_id or not stage_type:
                raise ValueError("Capability pipeline stages require stage_id and stage_type.")
            input_schema = self._pipeline_schema(
                normalized_id,
                stage_id,
                "input",
                raw.get("input_model"),
            )
            output_schema = self._pipeline_schema(
                normalized_id,
                stage_id,
                "output",
                raw.get("output_model"),
            )
            retry = raw.get("retry_policy") or {}
            agent_config = None
            batch_config = None
            if stage_type in {"agent", "direct_model"}:
                agent_config = AgentStageConfig(
                    agent_id=str(raw.get("agent_id") or "").strip(),
                    skill_package=str(raw.get("skill_package") or "").strip() or None,
                    primary_skill=str(raw.get("primary_skill") or "").strip() or None,
                    tools=tuple(_dedupe(list(raw.get("tools") or []))),
                    datasets=tuple(_dedupe(list(raw.get("datasets") or []))),
                    session_policy=str(raw.get("session_policy") or "isolated_stage"),
                    output_policy=str(raw.get("output_policy") or "strict"),
                )
            elif stage_type == "batch":
                item_schema = self._pipeline_schema(
                    normalized_id,
                    stage_id,
                    "item_output",
                    raw.get("item_output_model"),
                )
                batch_config = BatchStageConfig(
                    agent_id=str(raw.get("agent_id") or "").strip(),
                    item_source=str(raw.get("item_source") or "items").strip(),
                    skill_package=str(raw.get("skill_package") or "").strip() or None,
                    tools=tuple(_dedupe(list(raw.get("tools") or []))),
                    datasets=tuple(_dedupe(list(raw.get("datasets") or []))),
                    item_output_schema=item_schema,
                )
            definitions.append(
                StageDefinition(
                    stage_id=stage_id,
                    name=str(raw.get("name") or stage_id).strip(),
                    stage_type=stage_type,
                    depends_on=tuple(str(item) for item in raw.get("depends_on") or ()),
                    input_schema=input_schema,
                    output_schema=output_schema,
                    input_adapter=str(raw.get("input_adapter") or "pipeline_context"),
                    artifact_type=str(raw.get("artifact_type") or "").strip() or None,
                    timeout_seconds=int(raw.get("timeout_seconds") or 300),
                    retry_policy=RetryPolicy(
                        max_attempts=int(retry.get("max_attempts") or 1),
                        backoff_seconds=float(retry.get("backoff_seconds") or 0),
                        retry_on=tuple(str(item) for item in retry.get("retry_on") or ()),
                    ),
                    failure_policy=str(raw.get("failure_policy") or "fail_task"),
                    agent_config=agent_config,
                    batch_config=batch_config,
                    service_handler=str(raw.get("service_handler") or "").strip() or None,
                    requires_human_review=bool(raw.get("requires_human_review", False)),
                )
            )
        self._pipelines[normalized_id] = PipelineDefinition(
            pipeline_id=normalized_id,
            version=version.strip(),
            task_type=normalized_task_type,
            stages=tuple(definitions),
            description=description.strip(),
            final_artifact_type=final_artifact_type.strip() or "pipeline_result",
            timeout_seconds=int(timeout_seconds),
            resumable=bool(resumable),
            max_parallelism=int(max_parallelism),
        )

    def _pipeline_schema(
        self,
        pipeline_id: str,
        stage_id: str,
        kind: str,
        model: Any,
    ) -> str | None:
        if model is None:
            return None
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise ValueError(f"Capability pipeline {kind}_model must be a Pydantic model.")
        normalized = "_".join(
            part for part in f"{self.source_id}_{pipeline_id}_{stage_id}_{kind}".replace("-", "_").split(".")
            if part
        )
        schema_name = f"capability_{normalized}"
        if kind == "input":
            self._input_schemas[schema_name] = model
        else:
            self._output_schemas[schema_name] = model
        return schema_name

    def register_agent(
        self,
        *,
        agent_id: str,
        name: str,
        description: str = "",
        agent_type: str = "single",
        model_id: str = "deepseek-v4-pro",
        system_prompt: str = "",
        default_tools: list[str] | None = None,
        default_datasets: list[str] | None = None,
        runtime_config: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        normalized = agent_id.strip()
        if not normalized:
            raise ValueError("Capability agent_id is required.")
        if normalized in self._agents:
            raise ValueError(f"Agent '{normalized}' is declared more than once.")
        self._agents[normalized] = _AgentRegistration(
            agent_id=normalized,
            name=name.strip() or normalized,
            description=description.strip(),
            agent_type=agent_type.strip() or "single",
            model_id=model_id.strip() or "deepseek-v4-pro",
            system_prompt=system_prompt.strip(),
            default_tools=tuple(_dedupe(default_tools or [])),
            default_datasets=tuple(_dedupe(default_datasets or [])),
            runtime_config=dict(runtime_config or {}),
            enabled=enabled,
        )

    def register_skill_package(
        self,
        *,
        package_name: str,
        primary_skill: str,
        display_name: str = "",
        description: str = "",
        tags: list[str] | None = None,
        auxiliary_skills: list[str] | None = None,
        enabled: bool = True,
    ) -> None:
        normalized = package_name.strip()
        if not normalized:
            raise ValueError("Capability skill package name is required.")
        if normalized in self._skill_packages:
            raise ValueError(f"Skill package '{normalized}' is declared more than once.")
        if not primary_skill.strip():
            raise ValueError(f"Skill package '{normalized}' requires a primary skill.")
        self._skill_packages[normalized] = _SkillPackageRegistration(
            package_name=normalized,
            display_name=display_name.strip() or normalized,
            description=description.strip(),
            tags=tuple(_dedupe(tags or [])),
            primary_skill=primary_skill.strip(),
            auxiliary_skills=tuple(_dedupe(auxiliary_skills or [])),
            enabled=enabled,
        )

    def register_skill_root(self, path: str | Path) -> None:
        root = Path(path).expanduser().resolve()
        if root not in self._skill_roots:
            self._skill_roots.append(root)

    def register_http_tool(
        self,
        *,
        tool_name: str,
        provider: str,
        display_name: str,
        description: str,
        base_url: str,
        path: str,
        input_model: type[BaseModel],
        method: str = "POST",
        timeout_seconds: float = 30.0,
        max_response_chars: int = 100_000,
    ) -> None:
        normalized_name = tool_name.strip()
        normalized_provider = provider.strip()
        normalized_base_url = base_url.rstrip("/")
        normalized_path = path.strip()
        normalized_method = method.strip().upper()
        if not normalized_name or not normalized_provider:
            raise ValueError("HTTP tool name and provider are required.")
        parsed = urlsplit(normalized_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"HTTP tool '{normalized_name}' requires an http(s) base_url.")
        if not normalized_path.startswith("/") or "://" in normalized_path:
            raise ValueError(f"HTTP tool '{normalized_name}' path must be an absolute URL path.")
        if normalized_method != "POST":
            raise ValueError("Mounted HTTP tools currently support POST only.")
        if not isinstance(input_model, type) or not issubclass(input_model, BaseModel):
            raise ValueError(f"HTTP tool '{normalized_name}' input_model must be a Pydantic model.")
        if timeout_seconds <= 0 or max_response_chars <= 0:
            raise ValueError("HTTP tool limits must be positive.")

        registration = _HttpToolRegistration(
            tool_name=normalized_name,
            provider=normalized_provider,
            display_name=display_name.strip() or normalized_name,
            description=description.strip(),
            base_url=normalized_base_url,
            path=normalized_path,
            method=normalized_method,
            input_model=input_model,
            timeout_seconds=float(timeout_seconds),
            max_response_chars=int(max_response_chars),
        )
        if any(item.tool_name == normalized_name for item in self._http_tools.values()):
            raise ValueError(f"HTTP tool name '{normalized_name}' is declared more than once.")
        if registration.key in self._http_tools:
            raise ValueError(
                f"HTTP tool '{normalized_name}/{normalized_provider}' is declared more than once."
            )
        self._http_tools[registration.key] = registration

    async def apply(self, session: AsyncSession) -> dict[str, Any]:
        await self._validate_database_ownership(session)
        for pipeline_id, pipeline in self._pipelines.items():
            task = self._tasks.get(pipeline.task_type)
            if task is None or task.handler != "pipeline" or task.pipeline_id != pipeline_id:
                raise ValueError(
                    f"Capability pipeline '{pipeline_id}' must match a declared pipeline task."
                )
        for task in self._tasks.values():
            if task.handler == "pipeline" and task.pipeline_id not in self._pipelines:
                raise ValueError(
                    f"Capability task '{task.task_type}' references an undeclared pipeline."
                )

        for name, schema in self._input_schemas.items():
            register_input_schema(name, schema)
        for name, schema in self._output_schemas.items():
            register_output_schema(name, schema)
        for root in self._skill_roots:
            register_skill_root(root)
        for pipeline in self._pipelines.values():
            register_pipeline_definition(pipeline, source=self.source_id)
        for task in self._tasks.values():
            register_task_definition(task, source=self.source_id)
        for tool in self._http_tools.values():
            _register_http_tool_definition(tool, source_id=self.source_id)

        for agent in self._agents.values():
            runtime_config = {**dict(agent.runtime_config), _CAPABILITY_MARKER: self.source_id}
            await upsert_agent_profile(
                session,
                agent_id=agent.agent_id,
                name=agent.name,
                description=agent.description,
                agent_type=agent.agent_type,
                model_id=agent.model_id,
                system_prompt=agent.system_prompt,
                default_tools=list(agent.default_tools),
                default_datasets=list(agent.default_datasets),
                runtime_config=runtime_config,
                enabled=agent.enabled,
            )

        source_tag = _source_tag(self.source_id)
        for package in self._skill_packages.values():
            await upsert_skill_package(
                session,
                package_name=package.package_name,
                display_name=package.display_name,
                description=package.description,
                tags=_dedupe([*package.tags, source_tag]),
                primary_skill=package.primary_skill,
                auxiliary_skills=list(package.auxiliary_skills),
                enabled=package.enabled,
            )

        await self._upsert_tool_configs(session)
        return self.summary()

    async def _validate_database_ownership(self, session: AsyncSession) -> None:
        for agent in self._agents.values():
            existing = await get_agent_profile(session, agent.agent_id)
            if existing is not None and existing.runtime_config.get(_CAPABILITY_MARKER) != self.source_id:
                raise ValueError(
                    f"Agent '{agent.agent_id}' already exists and is not owned by "
                    f"capability '{self.source_id}'."
                )

        source_tag = _source_tag(self.source_id)
        for package in self._skill_packages.values():
            result = await session.exec(
                select(SkillPackageEntity).where(
                    SkillPackageEntity.tenant_id == DEFAULT_TENANT_ID,
                    SkillPackageEntity.package_name == package.package_name,
                )
            )
            existing = result.first()
            if existing is not None and source_tag not in existing.tags:
                raise ValueError(
                    f"Skill package '{package.package_name}' already exists and is not owned by "
                    f"capability '{self.source_id}'."
                )

        for tool in self._http_tools.values():
            result = await session.exec(
                select(ToolConfigEntity).where(
                    ToolConfigEntity.tenant_id == DEFAULT_TENANT_ID,
                    ToolConfigEntity.tool_name == tool.tool_name,
                    ToolConfigEntity.provider == tool.provider,
                )
            )
            existing = result.first()
            if existing is None:
                continue
            config = existing.to_provider_config().config
            if config.get(_CAPABILITY_MARKER) != self.source_id:
                raise ValueError(
                    f"Tool config '{tool.tool_name}/{tool.provider}' already exists and is not "
                    f"owned by capability '{self.source_id}'."
                )

    async def _upsert_tool_configs(self, session: AsyncSession) -> None:
        for tool in self._http_tools.values():
            result = await session.exec(
                select(ToolConfigEntity).where(
                    ToolConfigEntity.tenant_id == DEFAULT_TENANT_ID,
                    ToolConfigEntity.tool_name == tool.tool_name,
                    ToolConfigEntity.provider == tool.provider,
                )
            )
            entity = result.first()
            config_json = json.dumps(
                {
                    _CAPABILITY_MARKER: self.source_id,
                    "base_url": tool.base_url,
                    "path": tool.path,
                    "method": tool.method,
                    "timeout_seconds": tool.timeout_seconds,
                    "max_response_chars": tool.max_response_chars,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            if entity is None:
                entity = ToolConfigEntity(
                    tenant_id=DEFAULT_TENANT_ID,
                    tool_name=tool.tool_name,
                    provider=tool.provider,
                    enabled=True,
                    config_json=config_json,
                )
            else:
                entity.enabled = True
                entity.config_json = config_json
            session.add(entity)
        await session.commit()

    def summary(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "tasks": sorted(self._tasks),
            "pipelines": sorted(self._pipelines),
            "agents": sorted(self._agents),
            "skill_packages": sorted(self._skill_packages),
            "skill_roots": [str(path) for path in self._skill_roots],
            "tools": sorted(tool.tool_name for tool in self._http_tools.values()),
        }


async def mount_capability_entry(
    entry_path: str | Path,
    *,
    settings: CapabilitySettings | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Load exactly one explicitly configured Python entry and apply its declarations."""

    entry = Path(entry_path).expanduser().resolve()
    if not entry.is_file():
        raise ValueError(f"Capability entry does not exist or is not a file: {entry}")
    module = _load_entry_module(entry)
    source_id = str(getattr(module, "CAPABILITY_ID", "")).strip()
    if not source_id:
        raise ValueError(f"Capability entry '{entry}' must define CAPABILITY_ID.")
    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError(f"Capability entry '{entry}' must define callable register().")

    registry = CapabilityRegistry(source_id=source_id)
    registration_result = register(
        registry,
        settings or CapabilitySettings(dict(os.environ)),
    )
    if inspect.isawaitable(registration_result):
        await registration_result

    if session is not None:
        return await registry.apply(session)
    async with create_db_session() as owned_session:
        return await registry.apply(owned_session)


async def mount_capability_from_env(
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    entry = os.getenv(CAPABILITY_ENTRY_ENV, "").strip()
    if not entry:
        return None
    return await mount_capability_entry(entry, session=session)


async def mount_capabilities_from_env(
    *,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Mount the explicitly configured capability entries in declaration order."""

    entries = _capability_entries_from_env()
    summaries: list[dict[str, Any]] = []
    for entry in entries:
        summary = await mount_capability_entry(entry, session=session)
        summaries.append(summary)
        logger.info(
            "Mounted capability '{}' from '{}' with tools={} tasks={}.",
            summary["source_id"],
            entry,
            summary["tools"],
            summary["tasks"],
        )
    return summaries


def _capability_entries_from_env() -> list[Path]:
    raw_entries = os.getenv(CAPABILITY_ENTRIES_ENV, "").strip()
    if raw_entries:
        try:
            loaded = json.loads(raw_entries)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{CAPABILITY_ENTRIES_ENV} must be a JSON array of capability entry paths."
            ) from exc
        if not isinstance(loaded, list):
            raise ValueError(
                f"{CAPABILITY_ENTRIES_ENV} must be a JSON array of capability entry paths."
            )
        raw_paths = loaded
    else:
        legacy_entry = os.getenv(CAPABILITY_ENTRY_ENV, "").strip()
        raw_paths = [legacy_entry] if legacy_entry else []

    entries: list[Path] = []
    seen: set[Path] = set()
    for index, value in enumerate(raw_paths):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{CAPABILITY_ENTRIES_ENV}[{index}] must be a non-empty string path."
            )
        entry = Path(value).expanduser().resolve()
        if entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries


def _load_entry_module(entry: Path) -> ModuleType:
    digest = hashlib.sha256(str(entry).encode("utf-8")).hexdigest()[:16]
    module_name = f"_aituge_capability_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load capability entry: {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _register_http_tool_definition(
    registration: _HttpToolRegistration,
    *,
    source_id: str,
) -> None:
    tool_list = get_default_tool_list()
    existing = tool_list.providers_for(registration.tool_name)
    owned_keys = {
        key for key, owner in _TOOL_SOURCES.items() if owner == source_id
    }
    foreign = [item for item in existing if item.key not in owned_keys]
    if foreign:
        providers = ", ".join(sorted(item.provider for item in foreign))
        raise ValueError(
            f"Tool name '{registration.tool_name}' is already registered by provider(s): {providers}."
        )

    tool_list.register(
        ToolDefinition(
            tool_name=registration.tool_name,
            provider=registration.provider,
            display_name=registration.display_name,
            description=registration.description,
            llm_tool_names=(registration.tool_name,),
            factory=_http_tool_factory(registration),
        ),
        make_default=True,
    )
    _TOOL_SOURCES[registration.key] = source_id


def _http_tool_factory(registration: _HttpToolRegistration):
    def create_bundle(config: ToolProviderConfig) -> ToolBundle:
        raw = dict(config.config)
        base_url = str(raw.get("base_url") or registration.base_url).rstrip("/")
        path = str(raw.get("path") or registration.path)
        method = str(raw.get("method") or registration.method).upper()
        timeout_seconds = float(raw.get("timeout_seconds") or registration.timeout_seconds)
        max_response_chars = int(
            raw.get("max_response_chars") or registration.max_response_chars
        )

        async def invoke_http_tool(**payload: Any) -> str:
            try:
                async with httpx.AsyncClient(
                    base_url=base_url,
                    timeout=timeout_seconds,
                ) as client:
                    response = await client.request(method, path, json=payload)
                    response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500].strip()
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"{registration.tool_name} failed with HTTP "
                    f"{exc.response.status_code}{suffix}"
                ) from exc
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"{registration.tool_name} service is unavailable "
                    f"({exc.__class__.__name__})."
                ) from exc

            if len(response.text) > max_response_chars:
                raise RuntimeError(
                    f"{registration.tool_name} response exceeds the configured size limit."
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"{registration.tool_name} returned an invalid JSON response."
                ) from exc
            return json.dumps(result, ensure_ascii=False)

        tool = FunctionTool.from_defaults(
            async_fn=invoke_http_tool,
            name=registration.tool_name,
            description=registration.description,
            fn_schema=registration.input_model,
            return_direct=False,
        )
        return ToolBundle.from_tools([tool])

    return create_bundle


def _source_tag(source_id: str) -> str:
    return f"capability:{source_id}"


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _schema_prefix(source_id: str, task_type: str) -> str:
    normalized = "_".join(part for part in f"{source_id}_{task_type}".replace("-", "_").split(".") if part)
    return f"capability_{normalized}"


__all__ = [
    "CAPABILITY_ENTRY_ENV",
    "CAPABILITY_ENTRIES_ENV",
    "CapabilityRegistry",
    "CapabilitySettings",
    "mount_capabilities_from_env",
    "mount_capability_entry",
    "mount_capability_from_env",
]
