import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aituge_model.config import ResolvedSpeechRecognitionModel
from backend.speech_recognition import create_speech_recognition_router
from backend.speech_recognition.providers.aliyun_nls import vendor_event


def create_app(**kwargs):
    app = FastAPI()
    app.include_router(create_speech_recognition_router(**kwargs))
    return app


def configured_model(**overrides):
    values = {
        "id": "aliyun-nls-realtime",
        "mode": "api",
        "provider": "aliyun_nls",
        "model": "realtime-speech-recognition",
        "base_url": "wss://nls.example/ws/v1",
        "app_key": "test-app",
        "access_key_id": "",
        "access_key_secret": "",
        "token": "test-token",
        "region": "cn-shanghai",
        "meta_endpoint": "nls-meta.cn-shanghai.aliyuncs.com",
        "audio_format": "pcm",
        "sample_rate": 16000,
        "channels": 1,
        "recommended_chunk_ms": 20,
    }
    values.update(overrides)
    return ResolvedSpeechRecognitionModel(**values)


class FakeSpeechSession:
    def __init__(self, _model, emit):
        self.emit = emit
        self.audio = []
        self.shutdown_called = False

    def start(self):
        self.emit({"type": "ready", "vendor_event": "TranscriptionStarted"})

    def send_audio(self, data):
        self.audio.append(data)
        self.emit(
            {
                "type": "partial",
                "text": "这是临时结果",
                "vendor_event": "TranscriptionResultChanged",
            }
        )

    def stop(self):
        self.emit(
            {
                "type": "final",
                "text": "这是最终结果。",
                "vendor_event": "SentenceEnd",
            }
        )
        self.emit({"type": "completed", "vendor_event": "TranscriptionCompleted"})

    def shutdown(self):
        self.shutdown_called = True


def test_config_does_not_disclose_credentials():
    app = create_app(model=configured_model(), session_factory=FakeSpeechSession)
    client = TestClient(app)

    response = client.get("/api/speech/config")
    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "credentials_configured": True,
        "sdk_available": True,
        "model_id": "aliyun-nls-realtime",
        "provider": "aliyun_nls",
        "missing": [],
        "max_concurrent_sessions": 8,
        "audio": {
            "format": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "recommended_chunk_ms": 20,
        },
    }
    serialized = response.text
    assert "test-token" not in serialized
    assert "test-app" not in serialized


def test_websocket_bridges_pcm_and_streaming_transcript_events():
    sessions = []

    def factory(model, emit):
        session = FakeSpeechSession(model, emit)
        sessions.append(session)
        return session

    app = create_app(model=configured_model(), session_factory=factory)
    client = TestClient(app)

    with client.websocket_connect("/ws/speech") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        pcm = b"\x00\x00" * 320
        websocket.send_bytes(pcm)
        partial = websocket.receive_json()
        assert partial["type"] == "partial"
        assert partial["text"] == "这是临时结果"

        websocket.send_text(json.dumps({"type": "stop"}))
        final = websocket.receive_json()
        completed = websocket.receive_json()
        assert final["type"] == "final"
        assert final["text"] == "这是最终结果。"
        assert completed["type"] == "completed"

    assert sessions[0].audio == [pcm]
    assert sessions[0].shutdown_called is False


def test_websocket_reports_missing_server_configuration():
    app = create_app(
        model=configured_model(app_key="", token=""),
        session_factory=FakeSpeechSession,
    )
    client = TestClient(app)

    with client.websocket_connect("/ws/speech") as websocket:
        event = websocket.receive_json()

    assert event["type"] == "error"
    assert any("ALIYUN_NLS_APPKEY" in item for item in event["missing"])
    assert any("ALIYUN_NLS_TOKEN" in item for item in event["missing"])


def test_vendor_event_maps_nls_payload_without_forwarding_full_message():
    event = vendor_event(
        json.dumps(
            {
                "header": {
                    "name": "TranscriptionResultChanged",
                    "status": 20000000,
                    "status_text": "Gateway:SUCCESS:Success.",
                },
                "payload": {"result": "你好，世界"},
            }
        ),
        "partial",
    )

    assert event == {
        "type": "partial",
        "text": "你好，世界",
        "vendor_event": "TranscriptionResultChanged",
        "status": 20000000,
        "status_text": "Gateway:SUCCESS:Success.",
    }
