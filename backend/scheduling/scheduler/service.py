from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Optional

from common.system_constants import DEFAULT_TENANT_ID
from data.RAG.tool_retrieval import ToolRetrievalRAG
from db.db_context import create_db_session
from llama_index.core.tools.function_tool import FunctionTool
from service.agent import SingleAgentRunner, SingleAgentStreamEvent
from skill import SkillBundle, build_skill_bundle, create_read_skill_tool
from tool import ToolBundle, ToolProviderConfig, get_default_tool_list
from tool.registry import create_enabled_tool_bundle

from ..agent_registry.models import AgentProfileEntity

from .models import SchedulingChatRequest, SchedulingRuntimeOptions


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


def _skill_summary(skill) -> dict:
    return {
        "name": skill.name,
        "description": skill.description,
        "tags": skill.metadata.tags,
        "path": str(skill.path),
    }


def _skill_bundle_summary(bundle: SkillBundle) -> dict:
    if not bundle.primary and not bundle.candidates:
        return {}
    return {
        "primary": _skill_summary(bundle.primary) if bundle.primary else None,
        "candidates": [_skill_summary(skill) for skill in bundle.candidates],
    }


class SchedulingService:
    """Assembles one registered agent profile into an executable run."""

    def __init__(
        self,
        options: SchedulingRuntimeOptions,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        self.options = options
        self.tenant_id = tenant_id

    async def chat(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
    ):
        context = await self._build_context(profile, request)
        model_id = request.model or profile.model_id
        runner = SingleAgentRunner(default_model_id=model_id)
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
    ) -> AsyncIterator[SingleAgentStreamEvent]:
        context = await self._build_context(profile, request)
        model_id = request.model or profile.model_id
        runner = SingleAgentRunner(default_model_id=model_id)
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
    ) -> SchedulingToolContext:
        tool_names = _dedupe(profile.default_tools + request.extra_tools)
        dataset_names = _dedupe(profile.default_datasets + request.extra_datasets)
        bundle = await self._build_tool_bundle(tool_names, dataset_names)
        skill_context = self._build_skill_context(profile, request)
        task_prompts = [item for item in [profile.system_prompt, skill_context.task_prompt] if item]

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
    ) -> ToolBundle:
        bundles: list[ToolBundle] = []
        wants_code = "code_interpreter" in tool_names or "local_python" in tool_names
        wants_db_tools = bool(
            {"enabled_db_tools", "aliyun-websearch", "web_search", "search"}
            & set(tool_names)
        )
        wants_rag = "rag_retrieval" in tool_names or "local_rag" in dataset_names

        if wants_code:
            bundles.append(
                get_default_tool_list().create_bundle(
                    ToolProviderConfig(
                        tool_name="code_interpreter",
                        provider="local_python",
                        config={
                            "timeout_seconds": 20,
                            "max_output_chars": 50_000,
                            "work_dir": self.options.local_python_artifact_dir,
                            "artifact_base_url": self.options.artifact_base_url,
                            "keep_work_dir": True,
                        },
                    )
                )
            )

        if wants_db_tools:
            async with create_db_session() as session:
                bundles.append(
                    await create_enabled_tool_bundle(
                        session=session,
                        tenant_id=self.tenant_id,
                    )
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

    def _build_skill_context(
        self,
        profile: AgentProfileEntity,
        request: SchedulingChatRequest,
    ) -> SchedulingToolContext:
        if request.primary_skill or request.candidate_skills:
            primary_skill = request.primary_skill
            candidate_skills = request.candidate_skills or []
        else:
            profile_skills = _dedupe(profile.default_skills + request.extra_skills)
            primary_skill = profile_skills[0] if profile_skills else None
            candidate_skills = profile_skills[1:] if len(profile_skills) > 1 else []

        if not primary_skill and not candidate_skills:
            return SchedulingToolContext()

        bundle = build_skill_bundle(
            primary_skill=primary_skill,
            candidate_skills=candidate_skills,
        )
        read_skill_tool = create_read_skill_tool(bundle)
        return SchedulingToolContext(
            tools=[read_skill_tool] if read_skill_tool else [],
            task_prompt=bundle.render_prompt(),
            skills=_skill_bundle_summary(bundle),
        )
