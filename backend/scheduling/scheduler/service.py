from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Optional

from aituge_model.config import ModelRuntimeProvider
from common.system_constants import DEFAULT_TENANT_ID
from data.RAG.tool_retrieval import ToolRetrievalRAG
from llama_index.core.tools.function_tool import FunctionTool
from service.agent import SingleAgentRunner, SingleAgentStreamEvent
from skill import SkillManager
from tool import ToolBundle
from tool.artifacts import ArtifactPublisher
from tool.registry import ToolManager

from ..agent_registry.models import AgentProfileEntity

from .models import SchedulingChatRequest, SchedulingRuntimeOptions
from .runtime_context import SchedulingRuntimeContext


@dataclass(slots=True)
class SchedulingToolContext:
    tools: list[FunctionTool] = field(default_factory=list)
    task_prompt: str = ""
    skills: dict = field(default_factory=dict)
    cleanup: Optional[Callable] = None

    async def aclose(self) -> None:
        if self.cleanup is not None:
            result = self.cleanup()
            if result is not None:
                await result


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    values = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            values.append(item)
    return values


class SchedulingService:
    """Assembles one registered agent profile into an executable run."""

    def __init__(
        self,
        options: SchedulingRuntimeOptions,
        tenant_id: str = DEFAULT_TENANT_ID,
        model_pack_id: str | None = None,
    ) -> None:
        self.options = options
        self.tenant_id = tenant_id
        self.model_runtime_provider = ModelRuntimeProvider.from_environment(
            pack_id=model_pack_id or ""
        )
        self.model_pack_id = self.model_runtime_provider.active_pack.id
        self._task_pack_override = bool(str(model_pack_id or "").strip())

    async def chat(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
        *,
        runtime_context: SchedulingRuntimeContext | None = None,
        artifact_publisher: ArtifactPublisher | None = None,
    ):
        context = await self._build_context(
            profile,
            request,
            runtime_context=runtime_context,
            artifact_publisher=artifact_publisher,
        )
        model_id = request.model or (
            self.model_runtime_provider.active_pack.llm.id
            if self._task_pack_override
            else profile.model_id
        )
        runner = SingleAgentRunner(
            tenant_id=self.tenant_id,
            default_model_id=model_id,
            model_pack_id=self.model_pack_id,
        )
        try:
            result = await runner.chat(
                messages=request.messages,
                user_message=request.message,
                model_id=model_id,
                thread_id=request.thread_id,
                session_id=request.session_id,
                user_id=request.user_id,
                tools=context.tools,
                task_prompt=context.task_prompt,
            )
            body = result.model_dump()
            body["agent"] = profile.to_read_model()
            if context.skills:
                body["skills"] = context.skills
            return body
        finally:
            await context.aclose()

    async def stream_chat(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
        *,
        runtime_context: SchedulingRuntimeContext | None = None,
        artifact_publisher: ArtifactPublisher | None = None,
    ) -> AsyncIterator[SingleAgentStreamEvent]:
        context = await self._build_context(
            profile,
            request,
            runtime_context=runtime_context,
            artifact_publisher=artifact_publisher,
        )
        model_id = request.model or (
            self.model_runtime_provider.active_pack.llm.id
            if self._task_pack_override
            else profile.model_id
        )
        runner = SingleAgentRunner(
            tenant_id=self.tenant_id,
            default_model_id=model_id,
            model_pack_id=self.model_pack_id,
        )
        try:
            first = True
            async for event in runner.stream_chat(
                messages=request.messages,
                user_message=request.message,
                model_id=model_id,
                thread_id=request.thread_id,
                session_id=request.session_id,
                user_id=request.user_id,
                tools=context.tools,
                task_prompt=context.task_prompt,
            ):
                if first:
                    event.data = {
                        **(event.data or {}),
                        "agent": profile.to_read_model(),
                    }
                    if context.skills:
                        event.data["skills"] = context.skills
                    first = False
                yield event
        finally:
            await context.aclose()

    async def _build_context(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
        *,
        runtime_context: SchedulingRuntimeContext | None = None,
        artifact_publisher: ArtifactPublisher | None = None,
    ) -> SchedulingToolContext:
        tool_names = _dedupe(profile.default_tools + request.extra_tools)
        dataset_names = _dedupe(profile.default_datasets + request.extra_datasets)
        attachment_bundle, attachment_prompt, input_files = ToolBundle.empty(), "", {}
        from backend.attachments.tasks import enabled
        if request.attachments and enabled():
            from backend.attachments.tools import create_attachment_bundle
            attachment_bundle, attachment_prompt, input_files = await create_attachment_bundle(
                request.attachments, self.tenant_id, self.model_pack_id)
        bundle = await self._build_tool_bundle(
            tool_names,
            dataset_names,
            input_files=input_files,
            artifact_publisher=artifact_publisher,
        )
        bundle = ToolBundle.combine([attachment_bundle, bundle])
        skill_context = await SkillManager(tenant_id=self.tenant_id).create_context(
            request.skill_package
        )
        runtime_prompt = runtime_context.render_prompt() if runtime_context else ""
        task_prompts = [
            item
            for item in [profile.system_prompt, skill_context.task_prompt, runtime_prompt, attachment_prompt]
            if item
        ]

        return SchedulingToolContext(
            tools=bundle.tools + skill_context.tools,
            task_prompt="\n\n".join(task_prompts),
            skills=skill_context.skills,
            cleanup=bundle.cleanup,
        )

    async def _build_tool_bundle(
        self,
        tool_names: list[str],
        dataset_names: list[str],
        *,
        artifact_publisher: ArtifactPublisher | None = None,
        input_files: dict | None = None,
    ) -> ToolBundle:
        bundles: list[ToolBundle] = []
        wants_rag = "rag_retrieval" in tool_names or "local_rag" in dataset_names
        non_rag_tool_names = [name for name in tool_names if name != "rag_retrieval"]

        bundles.append(
            await ToolManager(
                local_python_work_dir=(
                    self.options.local_python_work_dir
                    or self.options.local_python_artifact_dir.parent / "code-runs"
                ),
                artifact_publisher=artifact_publisher,
                tenant_id=self.tenant_id,
                model_pack_id=self.model_pack_id,
                input_files=input_files,
            ).create_bundle(non_rag_tool_names)
        )
        if wants_rag:
            bundles.append(self._build_rag_bundle())

        return ToolBundle.combine(bundles)

    def _build_rag_bundle(self) -> ToolBundle:
        if self.options.rag_store is None:
            return ToolBundle.empty()
        knowledgebases, files, chunks = self.options.rag_store.load_models()
        if not knowledgebases:
            return ToolBundle.empty()
        rag_tools = ToolRetrievalRAG(
            knowledgebases=knowledgebases,
            files=files,
            chunks=chunks,
        ).create_tools()
        return ToolBundle.from_tools(rag_tools)
