from agent.react_agent import ReactAgent
import service.agent.single_agent_runner as runner_module
from service.agent.single_agent_runner import SingleAgentRunner


class _LlmMetadata:
    context_window = 4096
    max_tokens = 512


def _uninitialized_runner(runtime):
    runner = object.__new__(SingleAgentRunner)
    runner.llm_runtime = runtime
    return runner


def test_runner_binds_observability_runtime_to_real_react_agent(monkeypatch):
    runtime = object()
    runner = _uninitialized_runner(runtime)
    monkeypatch.setattr(runner_module, "ReactAgent", ReactAgent)

    agent = runner._create_agent(
        llm=_LlmMetadata(),
        system_prompt="system",
        tools=[],
        model_id="model-real",
    )

    assert type(agent) is ReactAgent
    assert agent.llm_runtime is runtime
    assert agent.model_id == "model-real"


def test_runner_preserves_legacy_injected_agent_constructor(monkeypatch):
    captured = {}

    class LegacyAgent:
        def __init__(self, llm, system_prompt, tools):
            captured.update(
                {
                    "llm": llm,
                    "system_prompt": system_prompt,
                    "tools": tools,
                }
            )

    runtime = object()
    llm = _LlmMetadata()
    runner = _uninitialized_runner(runtime)
    monkeypatch.setattr(runner_module, "ReactAgent", LegacyAgent)

    agent = runner._create_agent(
        llm=llm,
        system_prompt="legacy-system",
        tools=[],
        model_id="model-legacy",
    )

    assert type(agent) is LegacyAgent
    assert captured == {
        "llm": llm,
        "system_prompt": "legacy-system",
        "tools": [],
    }


def test_runner_does_not_mask_constructor_type_errors(monkeypatch):
    class BrokenAgent:
        def __init__(
            self,
            llm,
            system_prompt,
            tools,
            llm_runtime=None,
            model_id=None,
        ):
            raise TypeError("constructor defect")

    runner = _uninitialized_runner(object())
    monkeypatch.setattr(runner_module, "ReactAgent", BrokenAgent)

    try:
        runner._create_agent(
            llm=_LlmMetadata(),
            system_prompt="system",
            tools=[],
            model_id="model-broken",
        )
    except TypeError as exc:
        assert str(exc) == "constructor defect"
    else:
        raise AssertionError("constructor TypeError must propagate")
