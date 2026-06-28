from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


ROOT_DIR = Path(__file__).resolve().parent.parent
SINGLE_AGENT_DIR = ROOT_DIR / "backend" / "single-agent"
FRONTEND_DIR = ROOT_DIR / "frontend" / "simple-chat"

if str(SINGLE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SINGLE_AGENT_DIR))

from api.single_agent_api import router as single_agent_router  # noqa: E402


def create_app() -> FastAPI:
    app = FastAPI(title="TUGE Simple Chat")
    app.include_router(single_agent_router)
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    return app
