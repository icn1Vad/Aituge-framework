from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, List, Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from llama_index.core.tools.function_tool import FunctionTool
from pydantic import BaseModel
from service.agent import SingleAgentRunner
from service.conversation import ConversationManager
from skill import SkillBundle, build_skill_bundle, create_read_skill_tool, list_skills


class SingleAgentChatRequest(BaseModel):
    messages: Optional[List[dict]] = None
    message: Optional[str] = None
    model: str = "deepseek-v4-pro"
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: str = "default_user"
    stream: bool = False
    primary_skill: Optional[str] = None
    candidate_skills: Optional[List[str]] = None


CleanupHook = Callable[[], Awaitable[None] | None]
ToolProviderResult = Any
ToolProvider = Callable[[SingleAgentChatRequest], ToolProviderResult | Awaitable[ToolProviderResult]]


@dataclass(slots=True)
class SingleAgentToolContext:
    """Generic slot for tools selected outside the single-agent runtime."""

    tools: list[FunctionTool] = field(default_factory=list)
    task_prompt: str = ""
    skills: dict = field(default_factory=dict)
    cleanup: Optional[CleanupHook] = None

    async def aclose(self) -> None:
        if self.cleanup is None:
            return
        result = self.cleanup()
        if result is not None:
            await result


async def _maybe_await(value):
    if isawaitable(value):
        return await value
    return value


def _coerce_tool_context(value: ToolProviderResult) -> SingleAgentToolContext:
    if value is None:
        return SingleAgentToolContext()

    if isinstance(value, SingleAgentToolContext):
        return value

    if isinstance(value, tuple) and len(value) == 2:
        tools, cleanup = value
        return SingleAgentToolContext(tools=list(tools or []), cleanup=cleanup)

    if isinstance(value, SkillBundle):
        return _skill_context_from_bundle(value)

    if hasattr(value, "render_prompt") and not hasattr(value, "tools"):
        skills = _skill_bundle_summary(value) if isinstance(value, SkillBundle) else {}
        return SingleAgentToolContext(task_prompt=value.render_prompt(), skills=skills)

    if hasattr(value, "tools"):
        cleanup = getattr(value, "cleanup", None) or getattr(value, "aclose", None)
        task_prompt = ""
        if hasattr(value, "render_prompt"):
            task_prompt = value.render_prompt()
            skills = _skill_bundle_summary(value) if isinstance(value, SkillBundle) else {}
        elif hasattr(value, "task_prompt"):
            task_prompt = getattr(value, "task_prompt") or ""
            skills = getattr(value, "skills", {}) or {}
        else:
            skills = {}
        return SingleAgentToolContext(
            tools=list(getattr(value, "tools") or []),
            task_prompt=task_prompt,
            skills=skills,
            cleanup=cleanup,
        )

    return SingleAgentToolContext(tools=list(value))


def _merge_tool_contexts(*contexts: SingleAgentToolContext) -> SingleAgentToolContext:
    tools: list[FunctionTool] = []
    task_prompts: list[str] = []
    skills: dict = {}
    cleanups: list[CleanupHook] = []

    for context in contexts:
        tools.extend(context.tools)
        if context.task_prompt:
            task_prompts.append(context.task_prompt)
        if context.skills:
            skills = context.skills
        if context.cleanup:
            cleanups.append(context.cleanup)

    async def cleanup_all() -> None:
        for cleanup in reversed(cleanups):
            result = cleanup()
            if result is not None:
                await result

    return SingleAgentToolContext(
        tools=tools,
        task_prompt="\n\n".join(task_prompts),
        skills=skills,
        cleanup=cleanup_all if cleanups else None,
    )


def _skill_summary(skill):
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


def _skill_context_from_bundle(bundle: SkillBundle) -> SingleAgentToolContext:
    read_skill_tool = create_read_skill_tool(bundle)
    return SingleAgentToolContext(
        tools=[read_skill_tool] if read_skill_tool else [],
        task_prompt=bundle.render_prompt(),
        skills=_skill_bundle_summary(bundle),
    )


def _build_request_skill_context(request: SingleAgentChatRequest) -> SingleAgentToolContext:
    if not request.primary_skill and not request.candidate_skills:
        return SingleAgentToolContext()
    bundle = build_skill_bundle(
        primary_skill=request.primary_skill,
        candidate_skills=request.candidate_skills or [],
    )
    return _skill_context_from_bundle(bundle)


async def _build_tool_context(
    tool_provider: Optional[ToolProvider],
    request: SingleAgentChatRequest,
) -> SingleAgentToolContext:
    try:
        skill_context = _build_request_skill_context(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tool_provider is None:
        return skill_context
    provider_context = _coerce_tool_context(await _maybe_await(tool_provider(request)))
    return _merge_tool_contexts(skill_context, provider_context)


def create_router(tool_provider: Optional[ToolProvider] = None) -> APIRouter:
    router = APIRouter(prefix="/single-agent", tags=["single-agent"])

    @router.get("/skills")
    async def skills():
        return {
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "tags": skill.tags,
                    "path": str(skill.path),
                }
                for skill in list_skills()
            ]
        }

    @router.get("/threads")
    async def threads(user_id: str = "default_user", limit: int = 50, offset: int = 0):
        items = await ConversationManager().list_threads(
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
        return {
            "threads": [
                {
                    "id": thread.id,
                    "session_id": thread.id,
                    "title": thread.title,
                    "user_id": thread.user_id,
                    "created_at": thread.created_at.isoformat(),
                    "updated_at": thread.updated_at.isoformat(),
                }
                for thread in items
            ]
        }

    @router.get("/threads/{thread_id}/messages")
    async def thread_messages(thread_id: str):
        conversation = await ConversationManager().get_thread_messages(thread_id)
        if not conversation:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found.")
        return {
            "thread": {
                "id": conversation.thread.id,
                "session_id": conversation.thread.session_id,
                "title": conversation.thread.title,
                "user_id": conversation.thread.user_id,
                "created_at": conversation.thread.created_at.isoformat(),
                "updated_at": conversation.thread.updated_at.isoformat(),
            },
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "text": message.text,
                    "created_at": message.created_at.isoformat(),
                }
                for message in conversation.messages
            ],
        }

    @router.post("/chat")
    async def chat(request: SingleAgentChatRequest):
        runner = SingleAgentRunner(default_model_id=request.model)

        if request.stream:
            async def event_stream():
                tool_context = await _build_tool_context(tool_provider, request)
                try:
                    first = True
                    async for event in runner.stream_chat(
                        messages=request.messages,
                        user_message=request.message,
                        model_id=request.model,
                        thread_id=request.thread_id,
                        session_id=request.session_id,
                        user_id=request.user_id,
                        tools=tool_context.tools,
                        task_prompt=tool_context.task_prompt,
                    ):
                        if first and tool_context.skills:
                            event.data = {
                                **(event.data or {}),
                                "skills": tool_context.skills,
                            }
                            first = False
                        yield (
                            f"event: {event.event}\n"
                            f"data: {event.model_dump_json(exclude_none=True)}\n\n"
                        )
                finally:
                    await tool_context.aclose()

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        tool_context = await _build_tool_context(tool_provider, request)
        try:
            result = await runner.chat(
                messages=request.messages,
                user_message=request.message,
                model_id=request.model,
                thread_id=request.thread_id,
                session_id=request.session_id,
                user_id=request.user_id,
                tools=tool_context.tools,
                task_prompt=tool_context.task_prompt,
            )
            body = result.model_dump()
            if tool_context.skills:
                body["skills"] = tool_context.skills
            return body
        finally:
            await tool_context.aclose()

    return router


router = create_router()


def create_app(tool_provider: Optional[ToolProvider] = None) -> FastAPI:
    app = FastAPI(title="TUGE Single Agent Test API")
    app.include_router(create_router(tool_provider))
    return app
