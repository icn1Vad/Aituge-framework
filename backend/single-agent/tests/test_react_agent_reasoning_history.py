import asyncio

import agent.react_agent as react_agent_module
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


def test_return_direct_success_stops_without_another_llm_step():
    async def run():
        llm_calls = 0

        class FakeLlm:
            context_window = 4096
            max_tokens = 512

            async def astream(self, messages, tools=None):
                nonlocal llm_calls
                llm_calls += 1

                async def gen():
                    yield TextChunk(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_save",
                                type="function",
                                function={
                                    "name": "SaveTool",
                                    "arguments": '{"value": "final"}',
                                },
                            )
                        ]
                    )

                return gen()

        async def save(value: str) -> str:
            return value

        tool = FunctionTool.from_defaults(
            async_fn=save,
            name="SaveTool",
            description="Save a final value.",
            return_direct=True,
        )
        agent = ReactAgent(llm=FakeLlm(), system_prompt="system", tools=[tool], max_steps=3)
        response_gen = await agent.run_async(
            AgentState.from_messages([{"role": "user", "content": "save"}])
        )
        chunks = [chunk async for chunk in response_gen]

        assert llm_calls == 1
        assert chunks[-1].delta == "final"

    asyncio.run(run())


def test_return_direct_failure_continues_to_next_llm_step(monkeypatch):
    async def run():
        llm_calls = 0

        class FakeLlm:
            context_window = 4096
            max_tokens = 512

            async def astream(self, messages, tools=None):
                nonlocal llm_calls
                llm_calls += 1

                async def gen():
                    if llm_calls == 1:
                        yield TextChunk(
                            tool_calls=[
                                ChoiceDeltaToolCall(
                                    index=0,
                                    id="call_save",
                                    type="function",
                                    function={
                                        "name": "SaveTool",
                                        "arguments": '{"value": "final"}',
                                    },
                                )
                            ]
                        )
                    else:
                        yield TextChunk(delta="handled failure")

                return gen()

        async def save(value: str) -> str:
            return value

        async def failed_tool_call(tool_call, tool_fn_map):
            return tool_call, None, "write failed", "write failed"

        monkeypatch.setattr(react_agent_module, "execute_single_tool_call", failed_tool_call)
        tool = FunctionTool.from_defaults(
            async_fn=save,
            name="SaveTool",
            description="Save a final value.",
            return_direct=True,
        )
        agent = ReactAgent(llm=FakeLlm(), system_prompt="system", tools=[tool], max_steps=3)
        response_gen = await agent.run_async(
            AgentState.from_messages([{"role": "user", "content": "save"}])
        )
        chunks = [chunk async for chunk in response_gen]

        assert llm_calls == 2
        assert chunks[-1].delta == "handled failure"

    asyncio.run(run())
