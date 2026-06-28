import asyncio

from agent.react_agent import ReactAgent
from agent.state import AgentState
from common.llm.models import ReasoningChunk, TextChunk
from llama_index.core.tools.function_tool import FunctionTool
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall


def test_react_agent_keeps_reasoning_content_on_tool_call_message():
    async def run():
        seen_messages = []

        class FakeLlm:
            context_window = 4096
            max_tokens = 512

            async def astream(self, messages, tools=None):
                seen_messages.append(messages)

                async def gen():
                    if len(seen_messages) == 1:
                        yield ReasoningChunk(
                            reasoning_delta="need to call tool",
                            tool_calls=[
                                ChoiceDeltaToolCall(
                                    index=0,
                                    id="call_1",
                                    type="function",
                                    function={
                                        "name": "EchoTool",
                                        "arguments": '{"value": "hello"}',
                                    },
                                )
                            ],
                        )
                    else:
                        yield TextChunk(delta="done")

                return gen()

        async def echo(value: str) -> str:
            return value

        tool = FunctionTool.from_defaults(
            async_fn=echo,
            name="EchoTool",
            description="Echo a value.",
        )
        agent = ReactAgent(
            llm=FakeLlm(),
            system_prompt="system",
            tools=[tool],
            max_steps=3,
        )

        response_gen = await agent.run_async(
            AgentState.from_messages([{"role": "user", "content": "hello"}])
        )
        chunks = [chunk async for chunk in response_gen]

        assert chunks[-1].delta == "done"
        second_call_messages = seen_messages[1]
        assistant_tool_messages = [
            msg for msg in second_call_messages
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        ]
        assert assistant_tool_messages[0]["reasoning_content"] == "need to call tool"

    asyncio.run(run())

