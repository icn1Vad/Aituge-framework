from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


ROOT_DIR = Path(__file__).resolve().parent.parent
SINGLE_AGENT_DIR = ROOT_DIR / "backend" / "single-agent"
FRONTEND_DIR = ROOT_DIR / "frontend" / "simple-chat"
LOCAL_PYTHON_ARTIFACT_DIR = ROOT_DIR / "backend" / "tool" / "local_runtime" / "artifacts"

if str(SINGLE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SINGLE_AGENT_DIR))

from api.single_agent_api import ToolProvider, create_router  # noqa: E402


def create_app(tool_provider: ToolProvider | None = None) -> FastAPI:
    app = FastAPI(title="TUGE Simple Chat")
    app.include_router(create_router(tool_provider))
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
    app.mount(
        "/tool-artifacts/local-python",
        StaticFiles(directory=LOCAL_PYTHON_ARTIFACT_DIR),
        name="local-python-artifacts",
    )

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    return app
