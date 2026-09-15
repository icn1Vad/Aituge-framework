"""Provider-neutral contracts for streaming speech recognition."""

from collections.abc import Callable
from typing import Protocol

from aituge_model.config import ResolvedSpeechRecognitionModel


EventSink = Callable[[dict[str, object]], None]


class SpeechSession(Protocol):
    def start(self) -> None: ...

    def send_audio(self, data: bytes) -> None: ...

    def stop(self) -> None: ...

    def shutdown(self) -> None: ...


SessionFactory = Callable[
    [ResolvedSpeechRecognitionModel, EventSink],
    SpeechSession,
]


def missing_configuration(model: ResolvedSpeechRecognitionModel) -> list[str]:
    missing: list[str] = []
    if not model.app_key:
        missing.append("MODEL_SECRET_ALIYUN_NLS_APPKEY / ALIYUN_NLS_APPKEY")
    if not model.token and not (model.access_key_id and model.access_key_secret):
        missing.append(
            "MODEL_SECRET_ALIYUN_NLS_TOKEN，或 "
            "MODEL_SECRET_ALIYUN_ACCESS_KEY_ID + "
            "MODEL_SECRET_ALIYUN_ACCESS_KEY_SECRET"
        )
    return missing
