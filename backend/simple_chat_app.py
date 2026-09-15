from pathlib import Path
import os
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
SINGLE_AGENT_DIR = ROOT_DIR / "backend" / "single-agent"
FRONTEND_DIR = ROOT_DIR / "frontend" / "simple-chat"
AITUGE_TMP_ROOT = Path(
    os.environ.get("AITUGE_TMP_ROOT", ROOT_DIR.parent / "tmp")
).expanduser().resolve()
LOCAL_PYTHON_WORK_DIR = AITUGE_TMP_ROOT / "code-runs"
LOCAL_PYTHON_ARTIFACT_DIR = AITUGE_TMP_ROOT / "chat-artifacts"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SINGLE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SINGLE_AGENT_DIR))

from api.single_agent_api import ToolProvider, create_router  # noqa: E402
from capability_registry import create_capability_router  # noqa: E402
from backend.speech_recognition import (  # noqa: E402
    ResolvedSpeechRecognitionModel,
    SessionFactory,
    create_speech_recognition_router,
)


def create_app(
    tool_provider: ToolProvider | None = None,
    lifespan: Any = None,
    speech_recognition_model: ResolvedSpeechRecognitionModel | None = None,
    speech_recognition_session_factory: SessionFactory | None = None,
) -> FastAPI:
    LOCAL_PYTHON_WORK_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_PYTHON_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="TUGE Simple Chat", lifespan=lifespan)
    app.include_router(create_router(tool_provider))
    app.include_router(create_capability_router())
    app.include_router(
        create_speech_recognition_router(
            model=speech_recognition_model,
            session_factory=speech_recognition_session_factory,
        )
    )
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    return app
