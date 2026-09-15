"""Small ASGI entrypoint for the speech process used by Docker deployments."""

from fastapi import FastAPI

from .api import create_speech_recognition_router


def create_app() -> FastAPI:
    app = FastAPI(title="AI-Tuge Speech Recognition")
    app.include_router(create_speech_recognition_router())
    return app
