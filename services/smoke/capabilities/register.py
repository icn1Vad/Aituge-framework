"""Register the independent Smoke tool and Task with Aituge."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


CAPABILITY_ID = "smoke"


class SmokeEchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def non_blank_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


async def register(registry, settings) -> None:
    base_url = settings.require("SMOKE_SERVICE_BASE_URL")
    model_id = settings.get("SMOKE_MODEL_ID", "deepseek-v4-pro").strip()

    registry.register_http_tool(
        tool_name="smoke_echo",
        provider="smoke_http",
        display_name="Smoke Echo",
        description=(
            "Echo one message through the independent Aituge Smoke Service. "
            "The result includes the unchanged message and source=smoke-service."
        ),
        base_url=base_url,
        path="/v1/echo",
        method="POST",
        input_model=SmokeEchoInput,
        timeout_seconds=10,
        max_response_chars=20_000,
    )
    registry.register_agent(
        agent_id="smoke-echo-agent",
        name="Smoke Echo Agent",
        description="Verifies a real model can invoke a tool from a second capability entry.",
        agent_type="single",
        model_id=model_id or "deepseek-v4-pro",
        system_prompt=(
            "You are the Aituge multi-capability smoke-test agent. You must call smoke_echo "
            "exactly once with the user's complete message unchanged. After the tool succeeds, "
            "return a concise answer containing both the echoed message and the source value. "
            "Never invent a tool result and never answer before calling the tool."
        ),
        default_tools=["smoke_echo"],
        default_datasets=[],
    )
    registry.register_task(
        task_type="smoke.echo.chat",
        name="Smoke Echo Chat",
        description="Call the independently mounted Smoke Echo tool through a real model.",
        handler="scheduler",
        default_agent_id="smoke-echo-agent",
        default_tools=["smoke_echo"],
        default_datasets=[],
        conversation_message_field="message",
        input_model=SmokeEchoInput,
        stream_chunk_chars=24,
    )
