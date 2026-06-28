"""Simple chat app with the local Python runtime tool enabled."""

from pathlib import Path

from fastapi import FastAPI

from backend.simple_chat_app import create_app as create_simple_chat_app
from tool.local_runtime import LimitedLocalPythonConfig, create_limited_local_python_bundle


LOCAL_PYTHON_ARTIFACT_DIR = (
    Path(__file__).resolve().parent / "tool" / "local_runtime" / "artifacts"
)


def create_app() -> FastAPI:
    def tool_provider(_request):
        return create_limited_local_python_bundle(
            LimitedLocalPythonConfig(
                timeout_seconds=20,
                max_output_chars=50_000,
                work_dir=LOCAL_PYTHON_ARTIFACT_DIR,
                artifact_base_url="/tool-artifacts/local-python",
                keep_work_dir=True,
            )
        )

    return create_simple_chat_app(tool_provider=tool_provider)
