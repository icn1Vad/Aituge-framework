from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, List, Optional

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
from llama_index.core.tools.function_tool import FunctionTool
from pydantic import BaseModel
from service.agent import SingleAgentRunner


class SingleAgentChatRequest(BaseModel):
    messages: Optional[List[dict]] = None
    message: Optional[str] = None
    model: str = "deepseek-v4-pro"
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: str = "default_user"
    stream: bool = False


CleanupHook = Callable[[], Awaitable[None] | None]
ToolProviderResult = Any
ToolProvider = Callable[[SingleAgentChatRequest], ToolProviderResult | Awaitable[ToolProviderResult]]


@dataclass(slots=True)
class SingleAgentToolContext:
    """Generic slot for tools selected outside the single-agent runtime."""

    tools: list[FunctionTool] = field(default_factory=list)
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

    if hasattr(value, "tools"):
        cleanup = getattr(value, "cleanup", None) or getattr(value, "aclose", None)
        return SingleAgentToolContext(
            tools=list(getattr(value, "tools") or []),
            cleanup=cleanup,
        )

    return SingleAgentToolContext(tools=list(value))


async def _build_tool_context(
    tool_provider: Optional[ToolProvider],
    request: SingleAgentChatRequest,
) -> SingleAgentToolContext:
    if tool_provider is None:
        return SingleAgentToolContext()
    return _coerce_tool_context(await _maybe_await(tool_provider(request)))


def create_router(tool_provider: Optional[ToolProvider] = None) -> APIRouter:
    router = APIRouter(prefix="/single-agent", tags=["single-agent"])

    @router.post("/chat")
    async def chat(request: SingleAgentChatRequest):
        runner = SingleAgentRunner(default_model_id=request.model)

        if request.stream:
            async def event_stream():
                tool_context = await _build_tool_context(tool_provider, request)
                try:
                    async for event in runner.stream_chat(
                        messages=request.messages,
                        user_message=request.message,
                        model_id=request.model,
                        thread_id=request.thread_id,
                        session_id=request.session_id,
                        user_id=request.user_id,
                        tools=tool_context.tools,
                    ):
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
            )
            return result.model_dump()
        finally:
            await tool_context.aclose()

    return router


router = create_router()


def create_app(tool_provider: Optional[ToolProvider] = None) -> FastAPI:
    app = FastAPI(title="TUGE Single Agent Test API")
    app.include_router(create_router(tool_provider))
    return app
