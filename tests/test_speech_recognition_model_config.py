from aituge_model.config import ModelRuntimeProvider, load_model_registry


def test_default_registry_contains_aliyun_speech_recognition_model():
    registry = load_model_registry()
    registration = registry.speech_recognizers[registry.default_speech_recognition_id]

    assert registration.id == "aliyun-nls-realtime"
    assert registration.provider == "aliyun_nls"
    assert registration.base_url.startswith("wss://nls-gateway.")
    assert registration.app_key_ref == "aliyun_nls_appkey"
    assert registration.sample_rate == 16_000
    assert registration.channels == 1


def test_runtime_resolves_speech_model_metadata_and_secrets():
    provider = ModelRuntimeProvider.from_environment(
        secret_overrides={
            "aliyun_nls_appkey": "private-app-key",
            "aliyun_nls_token": "temporary-token",
        }
    )

    model = provider.resolve_speech_recognition()

    assert model.id == "aliyun-nls-realtime"
    assert model.provider == "aliyun_nls"
    assert model.app_key == "private-app-key"
    assert model.token == "temporary-token"
    assert model.access_key_id == ""
    assert model.access_key_secret == ""
