"""Deterministic HTTP endpoints for multi-capability smoke tests."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator


class EchoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def non_blank_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


app = FastAPI(title="Aituge Smoke Service", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {"success": True, "data": {"ok": True, "service": "smoke-service"}}


@app.post("/v1/echo")
async def echo(payload: EchoRequest) -> dict:
    return {
        "success": True,
        "data": {
            "message": payload.message,
            "source": "smoke-service",
        },
    }
