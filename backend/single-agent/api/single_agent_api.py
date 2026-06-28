from typing import List, Optional

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
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


router = APIRouter(prefix="/single-agent", tags=["single-agent"])


@router.post("/chat")
async def chat(request: SingleAgentChatRequest):
    runner = SingleAgentRunner(default_model_id=request.model)
    if request.stream:
        async def event_stream():
            async for event in runner.stream_chat(
                messages=request.messages,
                user_message=request.message,
                model_id=request.model,
                thread_id=request.thread_id,
                session_id=request.session_id,
                user_id=request.user_id,
                tools=[],
            ):
                yield f"event: {event.event}\ndata: {event.model_dump_json(exclude_none=True)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    result = await runner.chat(
        messages=request.messages,
        user_message=request.message,
        model_id=request.model,
        thread_id=request.thread_id,
        session_id=request.session_id,
        user_id=request.user_id,
        tools=[],
    )
    return result.model_dump()


def create_app() -> FastAPI:
    app = FastAPI(title="TUGE Single Agent Test API")
    app.include_router(router)
    return app
