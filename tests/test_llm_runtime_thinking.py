from service.conversation.llm_runner import _build_thinking_extra_body


class FakeLlm:
    def __init__(self, *, api_base: str, model: str, enable_thinking: bool) -> None:
        self.api_base = api_base
        self.model = model
        self.enable_thinking = enable_thinking


def test_explicitly_disables_thinking_for_official_deepseek_v4() -> None:
    llm = FakeLlm(
        api_base="https://api.deepseek.com",
        model="deepseek-v4-pro",
        enable_thinking=True,
    )

    assert _build_thinking_extra_body(llm, False) == {
        "thinking": {"type": "disabled"}
    }


def test_keeps_configured_behavior_when_contract_layer_does_not_override() -> None:
    llm = FakeLlm(
        api_base="https://api.deepseek.com",
        model="deepseek-v4-pro",
        enable_thinking=True,
    )

    assert _build_thinking_extra_body(llm, None) == {
        "chat_template_kwargs": {"enable_thinking": True},
        "enable_thinking": True,
    }


def test_uses_existing_compatible_fields_for_other_model_providers() -> None:
    llm = FakeLlm(
        api_base="https://example-model-provider.test/v1",
        model="another-model",
        enable_thinking=True,
    )

    assert _build_thinking_extra_body(llm, False) == {
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
    }
